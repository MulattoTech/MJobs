"""Request-boundary regressions using synthetic headers and no external services."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.main import BROWSER_SECURITY_HEADERS, LocalOriginMiddleware


PUBLIC_HOST = "mjobs.example.test"


def guarded_client(public_host=""):
    app = FastAPI()
    calls = []
    app.add_middleware(LocalOriginMiddleware, public_host=public_host)

    @app.api_route("/private-action", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def private_action(request: Request):
        calls.append((request.method, await request.body()))
        return {"private": "synthetic-private-record"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app), calls


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("referer", [
    "https://foreign.example/form?private=do-not-reflect",
    "http://testserver.foreign.example/",
    "http://testserver@foreign.example/",
    "http://testserver:9999/",
    "https://testserver/",
    "//testserver/path",
    "/relative/path",
    "null",
    "",
    "http://testserver:/",
    "http://testserver:0/",
    "http://testserver:65536/",
    "http://testserver:invalid/",
    "http://[broken/",
    "http://testserver\\@foreign.example/",
    "http://testserver/#fragment",
    " http://testserver/",
    "http://testserver/\tprivate",
])
def test_foreign_or_malformed_referrer_cannot_dispatch_private_request(method, referer):
    client, calls = guarded_client()
    with client:
        response = client.request(method, "/private-action", headers={"referer": referer})
    assert response.status_code == 403
    assert calls == []
    assert "synthetic-private-record" not in response.text
    assert "do-not-reflect" not in response.text
    for name, value in BROWSER_SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("referer", [
    "http://testserver/",
    "http://testserver/settings?tab=saved%20searches",
    "HTTP://TESTSERVER:80/profile",
])
def test_same_origin_referrer_allows_native_compatible_read_and_write(referer):
    client, calls = guarded_client()
    with client:
        for method in ("GET", "POST"):
            response = client.request(method, "/private-action", headers={"referer": referer})
            assert response.status_code == 200
    assert len(calls) == 2


@pytest.mark.parametrize(("name", "trusted", "foreign"), [
    ("host", "testserver", "foreign.example"),
    ("origin", "http://testserver", "https://foreign.example"),
    ("referer", "http://testserver/form", "https://foreign.example/form"),
    ("sec-fetch-site", "same-origin", "cross-site"),
])
@pytest.mark.parametrize("order", ["trusted-first", "foreign-first", "identical"])
def test_duplicate_security_headers_are_rejected_before_dispatch(name, trusted, foreign, order):
    values = [trusted, foreign]
    if order == "foreign-first":
        values.reverse()
    elif order == "identical":
        values = [trusted, trusted]
    client, calls = guarded_client()
    with client:
        response = client.post("/private-action", headers=[(name, value) for value in values],
                               content=b"synthetic-private-request")
    assert response.status_code == 400
    assert response.json() == {"detail": "Ambiguous request security headers"}
    assert calls == []
    assert "foreign.example" not in response.text
    for header, value in BROWSER_SECURITY_HEADERS.items():
        assert response.headers[header] == value


@pytest.mark.parametrize("headers", [
    {},
    {"sec-fetch-site": "none"},
])
def test_public_host_mutations_need_affirmative_same_origin_context(headers):
    client, calls = guarded_client(PUBLIC_HOST)
    with client:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = client.request(method, "/private-action",
                                      headers={"host": PUBLIC_HOST, **headers})
            assert response.status_code == 403
            assert response.json() == {"detail": "Same-origin browser context required"}
    assert calls == []


@pytest.mark.parametrize("headers", [
    {"origin": "https://" + PUBLIC_HOST},
    {"referer": "https://" + PUBLIC_HOST + "/profile?tab=contact"},
    {"referer": "https://" + PUBLIC_HOST + ":443/profile"},
    {"sec-fetch-site": "same-origin"},
])
def test_public_host_accepts_proven_same_origin_browser_context(headers):
    client, calls = guarded_client(PUBLIC_HOST)
    with client:
        response = client.post("/private-action", headers={"host": PUBLIC_HOST, **headers})
    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("headers", [
    {"referer": "http://" + PUBLIC_HOST + "/form"},
    {"referer": "https://" + PUBLIC_HOST + ".foreign.example/form"},
    {"referer": "https://" + PUBLIC_HOST + ":444/form"},
    {"referer": "https://" + PUBLIC_HOST + "/form", "sec-fetch-site": "same-site"},
    {"referer": "https://foreign.example/form", "sec-fetch-site": "same-origin"},
    {"origin": "null", "referer": "https://" + PUBLIC_HOST + "/form"},
    {"origin": "https://foreign.example", "referer": "https://" + PUBLIC_HOST + "/form"},
])
def test_other_headers_cannot_override_an_explicit_origin_or_fetch_denial(headers):
    client, calls = guarded_client(PUBLIC_HOST)
    with client:
        response = client.post("/private-action", headers={"host": PUBLIC_HOST, **headers})
    assert response.status_code == 403
    assert calls == []


def test_origin_takes_precedence_over_referrer_fallback():
    client, calls = guarded_client()
    with client:
        response = client.post("/private-action", headers={
            "origin": "http://testserver", "referer": "https://foreign.example/form",
        })
    assert response.status_code == 200
    assert len(calls) == 1


def test_local_headerless_native_clients_and_public_health_reads_remain_usable():
    client, calls = guarded_client(PUBLIC_HOST)
    with client:
        assert client.post("/private-action").status_code == 200
        assert client.get("/private-action", headers={
            "host": PUBLIC_HOST, "sec-fetch-site": "none",
        }).status_code == 200
        assert client.get("/health", headers={
            "host": PUBLIC_HOST, "referer": "https://foreign.example/",
        }).status_code == 200
    assert len(calls) == 2
