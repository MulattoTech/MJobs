import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tools.agent_dashboard.fleet_proxy import install_fleet_proxy


def test_fleet_proxy_routing_and_write_boundary():
    calls = []

    async def relay(method, tail, body, headers):
        calls.append((method, tail, body, headers))
        if tail == "":
            return httpx.Response(200, text='<script src="/app.js"></script><a href="/">Fleet</a>',
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, json={"ok": True})

    app = FastAPI()
    install_fleet_proxy(app, relay=relay)
    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("127.0.0.1", 50000)) as client:
        html = client.get("/fleet/").text
        assert 'src="/fleet/app.js"' in html
        assert 'href="/fleet/"' in html
        assert client.get("/fleet/api/preflight").status_code == 200
        baseline = len(calls)
        assert client.post("/fleet/api/nudge", json={}).status_code == 403
        assert client.post("/fleet/api/nudge", json={}, headers={
            "X-AO-Fleet": "1", "Origin": "https://attacker.example"}).status_code == 403
        assert client.get("/fleet/api/unknown").status_code == 404
        assert client.get("/fleet/api/state?target=http://example.test").status_code == 404
        assert len(calls) == baseline
        assert client.post("/fleet/api/agent-plan", json={"id": "fixture"},
                           headers={"X-AO-Fleet": "1",
                                    "Origin": "http://127.0.0.1:18729"}).status_code == 200
        assert calls[-1][1] == "api/agent-plan"
        assert calls[-1][3]["Origin"] == "http://127.0.0.1:8787"

    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("192.0.2.1", 50000)) as client:
        assert client.get("/fleet/api/state").status_code == 403


def test_fleet_proxy_timeout_does_not_retry():
    calls = []

    async def relay(*args):
        calls.append(args)
        raise httpx.ConnectError("unavailable")

    app = FastAPI()
    install_fleet_proxy(app, relay=relay)
    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("127.0.0.1", 50000)) as client:
        assert client.post("/fleet/api/nudge", json={},
                           headers={"X-AO-Fleet": "1"}).status_code == 503
    assert len(calls) == 1
