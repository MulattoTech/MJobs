import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from tools.agent_dashboard import fleet_proxy
from tools.agent_dashboard.fleet_proxy import install_fleet_proxy


REAL_ASYNC_CLIENT = httpx.AsyncClient


def recording_clients(monkeypatch, *, handler=None, events=None):
    """Inert upstreams; the browser side still exercises actual ASGI routes."""
    created = []

    class Client:
        def __init__(self, **options):
            self.options = options
            self.calls = []
            self.closed = False
            created.append(self)

        async def __aenter__(self):
            if events is not None:
                events.append("client_start")
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            if events is not None:
                events.append("client_stop")

        async def request(self, method, url, **kwargs):
            assert not self.closed
            self.calls.append((method, url, kwargs))
            if handler is not None:
                return await handler(self, method, url, kwargs)
            return httpx.Response(200, json={"client": created.index(self)})

    monkeypatch.setattr(fleet_proxy.httpx, "AsyncClient", Client)
    return created


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


def test_concurrent_assets_share_one_lifespan_client(monkeypatch):
    async def scenario():
        arrived = asyncio.Event()
        active = 0
        peak = 0

        async def response(client, method, url, kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if len(client.calls) == 9:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=2)
            active -= 1
            return httpx.Response(200, content=b"fixture", headers={
                "content-type": "application/javascript"})

        created = recording_clients(monkeypatch, handler=response)
        app = FastAPI()
        install_fleet_proxy(app)
        assert created == []
        assets = ["app.js", "turbo-ui.js", "sustain-ui.js", "master-ui.js",
                  "restore-ui.js", "quota-ui.js", "find-work-ui.js",
                  "startup-ui.js", "style.css"]
        async with app.router.lifespan_context(app):
            assert len(created) == 1
            options = created[0].options
            assert options["trust_env"] is False
            assert options["follow_redirects"] is False
            assert options["timeout"].connect == 2
            assert options["timeout"].read == 95
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with REAL_ASYNC_CLIENT(transport=transport,
                                         base_url="http://127.0.0.1:18729") as browser:
                responses = await asyncio.gather(*(
                    browser.get("/fleet/" + asset) for asset in assets))
            assert [response.status_code for response in responses] == [200] * 9
            assert len(created) == 1
            assert peak == 9
            assert {call[1] for call in created[0].calls} == {
                "http://127.0.0.1:8787/" + asset for asset in assets}
            assert not created[0].closed
        assert created[0].closed

    asyncio.run(scenario())


def test_existing_lifespan_state_start_and_close_are_preserved(monkeypatch):
    events = []

    @asynccontextmanager
    async def existing_lifespan(app):
        events.append("existing_start")
        try:
            yield {"existing_marker": "retained"}
        finally:
            events.append("existing_stop")

    created = recording_clients(monkeypatch, events=events)
    app = FastAPI(lifespan=existing_lifespan)

    @app.get("/existing-state")
    async def existing_state(request: Request):
        return {"marker": request.state.existing_marker}

    install_fleet_proxy(app)
    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("127.0.0.1", 50000)) as browser:
        assert browser.get("/existing-state").json() == {"marker": "retained"}
        assert browser.get("/fleet/api/health").status_code == 200
        assert events == ["existing_start", "client_start"]
    assert events == ["existing_start", "client_start", "client_stop", "existing_stop"]
    assert created[0].closed


def test_injected_relay_does_not_open_client_or_replace_lifespan(monkeypatch):
    def unexpected_client(**kwargs):
        raise AssertionError("Injected relay must not construct a real client")

    async def relay(method, tail, body, headers):
        return httpx.Response(200, json={"injected": True})

    monkeypatch.setattr(fleet_proxy.httpx, "AsyncClient", unexpected_client)
    app = FastAPI()
    original_lifespan = app.router.lifespan_context
    install_fleet_proxy(app, relay=relay)
    assert app.router.lifespan_context is original_lifespan
    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("127.0.0.1", 50000)) as browser:
        assert browser.get("/fleet/api/health").json() == {"injected": True}


def test_shared_client_failed_post_is_attempted_once(monkeypatch):
    async def unavailable(*args):
        raise httpx.ReadTimeout("Synthetic timeout after the POST was sent")

    created = recording_clients(monkeypatch, handler=unavailable)
    app = FastAPI()
    install_fleet_proxy(app)
    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("127.0.0.1", 50000)) as browser:
        response = browser.post("/fleet/api/turbo/toggle", json={"enabled": True},
                                headers={"X-AO-Fleet": "1",
                                         "Origin": "http://127.0.0.1:18729"})
        assert response.status_code == 503
        assert "no automatic retry" in response.json()["detail"]
    assert len(created) == 1
    assert len(created[0].calls) == 1
    assert created[0].calls[0][0] == "POST"
    assert created[0].calls[0][1] == "http://127.0.0.1:8787/api/turbo/toggle"


def test_shared_client_preserves_host_origin_and_loopback_guards(monkeypatch):
    created = recording_clients(monkeypatch)
    app = FastAPI()
    install_fleet_proxy(app)
    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("127.0.0.1", 50000)) as browser:
        assert browser.get("/fleet/api/health", headers={
            "Host": "attacker.example"}).status_code == 403
        assert browser.post("/fleet/api/turbo/toggle", json={}, headers={
            "X-AO-Fleet": "1", "Origin": "https://attacker.example"}).status_code == 403
        assert browser.post("/fleet/api/turbo/toggle", json={}, headers={
            "X-AO-Fleet": "1", "Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert browser.get("/fleet/api/health?target=http://example.test").status_code == 404
        assert created[0].calls == []
    with TestClient(app, base_url="http://127.0.0.1:18729",
                    client=("192.0.2.1", 50000)) as browser:
        assert browser.get("/fleet/api/health").status_code == 403
    assert all(client.calls == [] for client in created)


def test_clients_are_not_shared_between_app_lifespans(monkeypatch):
    async def scenario():
        created = recording_clients(monkeypatch)
        first, second = FastAPI(), FastAPI()
        install_fleet_proxy(first)
        install_fleet_proxy(second)
        async with first.router.lifespan_context(first):
            async with second.router.lifespan_context(second):
                assert len(created) == 2
                async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(
                    app=first, client=("127.0.0.1", 50000)),
                    base_url="http://127.0.0.1:18729",
                ) as browser:
                    assert (await browser.get("/fleet/api/health")).json() == {"client": 0}
                async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(
                    app=second, client=("127.0.0.1", 50000)),
                    base_url="http://127.0.0.1:18729",
                ) as browser:
                    assert (await browser.get("/fleet/api/health")).json() == {"client": 1}
            assert created[1].closed
            assert not created[0].closed
        assert all(client.closed for client in created)

    asyncio.run(scenario())
