import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.agent_dashboard.fleet_proxy import install_fleet_proxy


@pytest.mark.parametrize("tail", ["api/startup", "startup-ui.js", "startup.css"])
def test_startup_reads_use_the_same_guarded_port(tail):
    calls = []

    async def relay(method, path, body, headers):
        calls.append((method, path))
        return httpx.Response(200, json={"run": {"phase": "idle"}})

    app = FastAPI()
    install_fleet_proxy(app, relay)
    with TestClient(app, base_url="http://127.0.0.1:18729", client=("127.0.0.1", 50000)) as client:
        assert client.get("/fleet/" + tail).status_code == 200
    assert calls == [("GET", tail)]


@pytest.mark.parametrize("tail", ["api/startup", "api/startup/cancel", "api/startup/reconcile", "api/orchestrator/open"])
def test_startup_writes_keep_origin_header_and_no_retry_guards(tail):
    calls = []

    async def relay(method, path, body, headers):
        calls.append((method, path, headers))
        raise httpx.ConnectError("fixture unavailable")

    app = FastAPI()
    install_fleet_proxy(app, relay)
    with TestClient(app, base_url="http://127.0.0.1:18729", client=("127.0.0.1", 50000)) as client:
        assert client.post("/fleet/" + tail, json={}).status_code == 403
        assert client.post("/fleet/" + tail, json={}, headers={"X-AO-Fleet": "1", "Origin": "https://other.test"}).status_code == 403
        assert calls == []
        assert client.post("/fleet/" + tail, json={}, headers={"X-AO-Fleet": "1", "Origin": "http://127.0.0.1:18729"}).status_code == 503
    assert len(calls) == 1
    assert calls[0][:2] == ("POST", tail)
    assert calls[0][2]["Origin"] == "http://127.0.0.1:8787"
