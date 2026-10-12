from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from tools.agent_dashboard.observability import opencode_usage_snapshot, read_codex_rate_limits
from tools.agent_dashboard.server import create_app
from tools.agent_dashboard.supervisor import AgentSupervisor


DIAGNOSTIC = "SYNTHETIC_PRIVATE_TELEMETRY C:/private/config Traceback"
CODEX_ERROR = "Codex usage is unavailable. Try refreshing again."
OPENCODE_ERROR = "OpenCode usage is unavailable. Try refreshing again."


def codex_process(result):
    messages = [{"id": 1, "result": {}}, {"id": 2, **result}]
    return SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO("".join(json.dumps(x) + "\n" for x in messages)),
        stderr=io.StringIO(), terminate=Mock(), wait=Mock(), kill=Mock(),
    )


@pytest.mark.parametrize("error", [OSError, PermissionError, FileNotFoundError])
def test_codex_start_failure_returns_safe_unavailable_http_state(tmp_path, error):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    exe = tmp_path / "synthetic-codex.exe"
    exe.write_text("synthetic fixture", encoding="utf-8")
    with patch.object(AgentSupervisor, "_monitor_loop", return_value=None):
        app = create_app(tmp_path)
    supervisor = app.state.supervisor
    supervisor._telemetry_cache.update({
        "processes": (time.monotonic(), []),
        "gpu": (time.monotonic(), {"available": False}),
        "ollama_ps": (time.monotonic(), {"available": False, "models": []}),
    })
    launch = Mock(side_effect=error(DIAGNOSTIC))
    with patch("tools.agent_dashboard.observability._native_codex_exe", return_value=exe), \
         patch("tools.agent_dashboard.observability.subprocess",
               SimpleNamespace(Popen=launch, PIPE=subprocess.PIPE)), TestClient(app) as client:
        response = client.get("/api/state")
    assert response.status_code == 200
    assert DIAGNOSTIC not in response.text
    assert response.json()["lanes"]["codex"]["usage"] == {"available": False, "error": CODEX_ERROR}
    launch.assert_called_once()


@pytest.mark.parametrize("result", [
    {"result": {"rateLimits": {"primary": {"usedPercent": DIAGNOSTIC}}}},
    {"error": {"message": DIAGNOSTIC, "data": {"trace": DIAGNOSTIC}}},
    {"result": {"rateLimits": {"secondary": {"usedPercent": DIAGNOSTIC}}}},
])
def test_codex_diagnostics_are_safe_through_http_and_recover(tmp_path, result):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    exe = tmp_path / "synthetic-codex.exe"
    exe.write_text("synthetic fixture", encoding="utf-8")
    failed = codex_process(result)
    healthy = codex_process({"result": {"rateLimits": {
        "primary": {"usedPercent": 0, "windowDurationMins": 300},
        "secondary": {"usedPercent": 25}, "planType": "synthetic",
    }}})
    launch = Mock(side_effect=[failed, healthy])
    with patch("tools.agent_dashboard.observability._native_codex_exe", return_value=exe), \
         patch("tools.agent_dashboard.observability.subprocess",
               SimpleNamespace(Popen=launch, PIPE=subprocess.PIPE, run=subprocess.run)):
        unavailable = read_codex_rate_limits(timeout=1)
        assert unavailable == {"available": False, "error": CODEX_ERROR}
        with patch.object(AgentSupervisor, "_monitor_loop", return_value=None):
            app = create_app(tmp_path)
        supervisor = app.state.supervisor
        supervisor._telemetry_cache.update({
            "codex_usage": (time.monotonic(), unavailable),
            "processes": (time.monotonic(), []),
            "gpu": (time.monotonic(), {"available": False}),
            "ollama_ps": (time.monotonic(), {"available": False, "models": []}),
        })
        with TestClient(app) as client, patch.object(supervisor, "start") as start:
            response = client.get("/api/state")
            assert response.status_code == 200
            assert DIAGNOSTIC not in response.text
            assert response.json()["lanes"]["codex"]["usage"] == unavailable
            # Use the actual refresh/cache path; zero usage must be available.
            supervisor._refresh_telemetry_async("codex_usage", read_codex_rate_limits)
            response = client.get("/api/state")
            usage = response.json()["lanes"]["codex"]["usage"]
            assert usage["available"] is True
            assert usage["used_percent"] == 0
            assert usage["remaining_percent"] == 100
            assert usage["secondary_remaining_percent"] == 75
            assert "error" not in usage
            start.assert_not_called()
        assert launch.call_count == 2
    failed.terminate.assert_called_once()
    healthy.terminate.assert_called_once()
    assert failed.stdout.closed and healthy.stdout.closed


def test_opencode_database_diagnostics_are_safe_and_recover(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    db = tmp_path / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as con:
        con.executescript("""
            CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT, directory TEXT);
            CREATE TABLE message (session_id TEXT, time_created INT, data TEXT);
            CREATE TABLE part (session_id TEXT, data TEXT);
        """)
        con.execute("INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                    ("s1", "fixture", 1000, 2000, tmp_path.as_posix()))
        con.execute("INSERT INTO message VALUES (?, ?, ?)", ("s1", 1000,
                    json.dumps({"role": "assistant", "tokens": {"input": DIAGNOSTIC}})))
    with patch("tools.agent_dashboard.observability.Path.home", return_value=tmp_path):
        unavailable = opencode_usage_snapshot(tmp_path)
        assert unavailable == {"available": False, "error": OPENCODE_ERROR}
    with patch.object(AgentSupervisor, "_monitor_loop", return_value=None):
        app = create_app(tmp_path)
    supervisor = app.state.supervisor
    supervisor.runtime["lanes"]["ollama"]["workspace"] = str(tmp_path)
    supervisor._telemetry_cache.update({
        "codex_usage": (time.monotonic(), {"available": False}),
        "processes": (time.monotonic(), []),
        "gpu": (time.monotonic(), {"available": False}),
        "ollama_ps": (time.monotonic(), {"available": False, "models": []}),
        f"opencode_usage:{tmp_path}": (time.monotonic(), unavailable),
    })
    with TestClient(app) as client:
        response = client.get("/api/state")
        assert response.status_code == 200
        assert DIAGNOSTIC not in response.text
        assert response.json()["lanes"]["ollama"]["usage"]["error"] == OPENCODE_ERROR
    with patch("tools.agent_dashboard.observability.Path.home", return_value=tmp_path):
        with sqlite3.connect(db) as con:
            con.execute("DELETE FROM message")
        recovered = opencode_usage_snapshot(tmp_path)
    assert recovered["available"] is True
    assert recovered["session_count"] == 1
    assert recovered["current_session"]["input_tokens"] == 0
    assert "error" not in recovered
