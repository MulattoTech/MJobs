"""Read-only external-sprint discovery must never grant Mission Control PID ownership."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.agent_dashboard.external_sprints import current_round3


def event(kind, payload):
    return json.dumps({"type": kind, "payload": payload}, separators=(",", ":"))


@pytest.fixture
def fixture(tmp_path):
    repo = tmp_path / "MJobs"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    clone = repo / ".mjobs-recovery" / "active" / "MJobs-Round3-20261014"
    clone.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "switch", "-q", "-c", "codex/p0p1-round3"], check=True)
    state_dir = repo / ".git" / "mjobs-p0p1-round3"
    state_dir.mkdir()
    (state_dir / "latest.json").write_text(json.dumps({
        "worktree": str(clone), "campaign": "round3-20261014",
        "status": "RUNNING", "branch": "codex/p0p1-round3",
    }))
    goal = clone / ".git" / "afk-dev"
    goal.mkdir()
    (goal / "goal.json").write_text(json.dumps({
        "statement": "Finish P0/P1 requirements"
    }))
    (clone / "new-source.py").write_text("print('hello')\n")
    sessions = tmp_path / "sessions"
    day = sessions / "2026" / "10" / "14"
    day.mkdir(parents=True)
    (day / "rollout-2026-10-14T15-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl").write_text(
        "\n".join([
            event("session_meta", {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                                   "cwd": str(clone), "source": "cli"}),
            event("turn_context", {"model": "gpt-6.1-sol", "effort": "high"}),
            event("response_item", {"type": "function_call", "name": "spawn_agent"}),
            event("event_msg", {"type": "token_count", "info": {
                "total_token_usage": {
                    "input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 40}}}),
        ]) + "\n", encoding="utf8")
    (day / "rollout-2026-10-14T15-01-00-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl").write_text(
        "\n".join([
            event("session_meta", {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "cwd": str(clone),
                "source": {"subagent": {"thread_spawn": {
                    "parent_thread_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "agent_nickname": "Tesla"}}}}),
            event("turn_context", {"model": "gpt-6-luna", "effort": "high"}),
            event("event_msg", {"type": "token_count", "info": {
                "total_token_usage": {
                    "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}}}),
        ]) + "\n", encoding="utf8")
    return repo, clone, sessions, day


def test_active_external_codex_discovered_without_ownership(fixture):
    repo, clone, sessions, _ = fixture
    process = {"Name": "codex.exe", "ProcessId": 5522,
               "CommandLine": f"codex.exe -C {clone} -m gpt-6.1-sol"}
    result = current_round3(repo, session_root=sessions, processes=[process])
    assert result["available"] and result["read_only"]
    assert result["observation"] == "RUNNING"
    assert result["pids"] == [5522]
    assert result["branch"] == "codex/p0p1-round3"
    assert result["changed_count"] == 1
    assert result["subagent_count"] == 1
    assert {x["name"] for x in result["sessions"]} == {"Root agent", "Tesla"}
    assert result["root_session"]["model"] == "gpt-6.1-sol"
    assert result["tokens"] == {"input_tokens": 400, "cached_tokens": 280,
                                "output_tokens": 60}
    assert result["goal"] == "Finish P0/P1 requirements"
    assert "stop" in result["note"].lower()  # Observation must never imply ownership.
    assert not (clone / "new-source.py").read_text() == ""


def test_stopped_process_is_not_misrepresented_as_running(fixture):
    repo, _, sessions, _ = fixture
    result = current_round3(repo, session_root=sessions, processes=[])
    assert result["checkpoint_status"] == "RUNNING"
    assert result["observation"] == "INACTIVE"
    assert result["pids"] == []


def test_wrong_workspace_session_is_excluded(fixture):
    repo, _, sessions, day = fixture
    (day / "rollout-2026-10-14T15-02-00-cccccccc-cccc-cccc-cccc-cccccccccccc.jsonl").write_text(
        event("session_meta", {"cwd": str(repo / "WrongWorkspace"),
                               "id": "cccccccc-cccc-cccc-cccc-cccccccccccc"}) + "\n",
        encoding="utf8",
    )
    result = current_round3(repo, session_root=sessions, processes=[])
    assert len(result["sessions"]) == 2


def test_outside_checkout_is_rejected_without_access(tmp_path):
    repo = tmp_path / "MJobs"
    state_dir = repo / ".git" / "mjobs-p0p1-round3"
    state_dir.mkdir(parents=True)
    outside = tmp_path / "Elsewhere"
    outside.mkdir()
    (outside / ".git").mkdir()
    (state_dir / "latest.json").write_text(json.dumps({"worktree": str(outside)}))
    result = current_round3(repo, session_root=tmp_path, processes=[])
    assert result["available"] is False
    assert result["reason"] == "Campaign is outside the canonical archive"


def test_missing_campaign_gives_clear_not_available(tmp_path):
    assert current_round3(tmp_path, processes=[])["available"] is False


def test_observer_does_not_change_checkout_or_checkpoint(fixture):
    repo, clone, sessions, _ = fixture
    files = {p: p.read_bytes() for p in (
        repo / ".git" / "mjobs-p0p1-round3" / "latest.json",
        clone / "new-source.py", clone / ".git" / "afk-dev" / "goal.json"
    )}
    current_round3(repo, session_root=sessions, processes=[])
    assert all(p.read_bytes() == original for p, original in files.items())

def test_mission_control_endpoint_reports_only_readonly_observer(fixture, monkeypatch):
    from fastapi.testclient import TestClient
    from tools.agent_dashboard import server as dashboard_server

    repo, _, _, _ = fixture
    monkeypatch.setattr(
        dashboard_server, "current_round3",
        lambda _root, processes: {
            "available": True,
            "read_only": True,
            "observation": "RUNNING",
            "sessions": [{"name": "Tesla", "model": "gpt-6-luna"}],
        },
    )
    monkeypatch.setattr(dashboard_server, "windows_process_snapshot", lambda: [])
    monkeypatch.setattr(
        dashboard_server, "read_codex_rate_limits",
        lambda: {"available": True, "used_percent": 73, "remaining_percent": 27},
    )
    app = dashboard_server.create_app(repo, mission_instance=True)
    with TestClient(app) as client:
        reply = client.get("/api/sprints/active")
        assert reply.status_code == 200
        assert reply.json()["sessions"][0]["name"] == "Tesla"
        assert reply.json()["read_only"] is True
        assert reply.json()["quota"]["used_percent"] == 73
        # Read-only discovery must not create stop/restart controls for external agents.
        assert client.get("/api/launcher/health").status_code == 200
        token = app.state.mission_control.token
        payload = {"profile": "codex-sol-6-1-high", "model": "gpt-6.1-sol",
                   "effort": "high", "max_subagents": 4,
                   "intended_subagents": 2}
        blocked = client.post(
            "/api/launcher/codex/launch",
            headers={"X-MJobs-Launch-Token": token}, json=payload,
        )
        assert blocked.status_code == 409
        assert "already running" in blocked.text
