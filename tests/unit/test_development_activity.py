"""Mission Control shows GitHub delivery without inferring acceptance or CLI sessions."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tools.agent_dashboard import development_activity, server


GOOD = {
    "available": True, "source": "GitHub public MJobs PR API",
    "openTotal": 51, "openDrafts": 1, "mergedSince": 70,
    "sinceUTC": "2026-11-02", "observedAt": "2026-11-09T20:27:51.829Z",
}


def transport_for(payload, *, status=200):
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:4320/api/delivery"
        assert request.method == "GET"
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def test_valid_hub_delivery_is_copied_as_activity_not_acceptance():
    result = development_activity.read_development_activity(transport_for(GOOD))
    assert result["available"] is True
    assert result["openTotal"] == 51
    assert result["openDrafts"] == 1
    assert result["mergedSince"] == 70
    assert "accepted requirements" in result["note"]
    assert "passed" not in result
    assert "sessions" not in result


@pytest.mark.parametrize("bad", [
    {"available": False, "reason": "agent quota unavailable"},
    {**GOOD, "openTotal": 0, "openDrafts": 3},
    {**GOOD, "openTotal": "51"},
    {**GOOD, "mergedSince": True},
    {**GOOD, "source": "unexpected"},
    {**GOOD, "sinceUTC": "invalid"},
    {**GOOD, "observedAt": "bad"},
    {"available": True, "openTotal": 0},
])
def test_malformed_or_offline_source_never_infers_zero(bad):
    result = development_activity.read_development_activity(transport_for(bad))
    assert result["available"] is False
    assert "openTotal" not in result
    assert "reason" in result


def test_http_failures_redirects_and_oversized_data_fail_closed():
    for status in (302, 403, 429, 500):
        result = development_activity.read_development_activity(
            transport_for(GOOD, status=status))
        assert result["available"] is False
    large = httpx.MockTransport(lambda _: httpx.Response(
        200, content=json.dumps({**GOOD, "padding": "x" * 9000})))
    assert development_activity.read_development_activity(large)["available"] is False
    missing = httpx.MockTransport(lambda request: (_ for _ in ()).throw(
        httpx.ConnectError("private diagnostics", request=request)))
    result = development_activity.read_development_activity(missing)
    assert result["available"] is False
    assert "private diagnostics" not in str(result)


def test_same_origin_route_and_page_show_web_pro_distinction(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in ("PARALLEL_CODEX_LANE.md", "PARALLEL_ANTIGRAVITY_LANE.md", "PARALLEL_OLLAMA_LANE.md"):
        (prompts / name).write_text("PROMPT\n", encoding="utf-8")
    monkeypatch.setattr(server, "read_development_activity", lambda: dict(GOOD))
    with TestClient(server.create_app(tmp_path)) as client:
        result = client.get("/api/development-activity")
        assert result.status_code == 200
        assert result.json()["openTotal"] == 51
        assert client.get("/api/development-activity",
                          headers={"Host": "evil.example"}).status_code == 400
        page = client.get("/").text
        assert 'id="developmentActivity"' in page
        assert "ChatGPT Web Pro" in page
        assert "local session counts do not measure it" in page
    script = (Path(server.__file__).parent / "static" / "mission.js").read_text(encoding="utf-8")
    assert 'fetchJson("/api/development-activity"' in script
    assert "refreshDevelopmentActivity()" in script
    assert "developmentInflight" in script
    assert "GitHub delivery unavailable" in script
