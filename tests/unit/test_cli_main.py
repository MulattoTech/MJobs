"""Bounded, read-only local maintenance CLI contract (MJ-REQ-207)."""
from __future__ import annotations

import json

import httpx
import pytest

from cli import __main__ as maintenance


def _transport(handler):
    return httpx.MockTransport(handler)


def test_health_get_only_and_redacted_output(capsys):
    observed = []

    def handler(request):
        observed.append((request.method, request.url.host, request.url.path))
        return httpx.Response(200, json={
            "status": "ok", "check": "liveness", "mode": "LOCAL_FIRST",
            "operation_mode": "FIXTURE", "external_submission_enabled": False,
            "secret_internal": "DO_NOT_PRINT",
        })

    assert maintenance.main(["health"], transport=_transport(handler)) == 0
    output = capsys.readouterr()
    assert observed == [("GET", "127.0.0.1", "/health")]
    assert json.loads(output.out) == {
        "check": "liveness", "external_submission_enabled": False,
        "mode": "LOCAL_FIRST", "operation_mode": "FIXTURE", "status": "ok",
    }
    assert "DO_NOT_PRINT" not in output.out + output.err


def test_source_list_whitelists_fields_without_credentials_or_urls(capsys):
    observed = []

    def handler(request):
        observed.append((request.method, request.url.path))
        return httpx.Response(200, json={"sources": [{
            "id": "source-1", "name": "Fixture Jobs", "provider": "greenhouse",
            "enabled": True, "health_status": "HEALTHY",
            "private_token": "DO_NOT_PRINT", "endpoint": "https://private.example/token",
        }]})

    assert maintenance.main(["source", "list"], transport=_transport(handler)) == 0
    output = capsys.readouterr()
    assert observed == [("GET", "/sources")]
    assert json.loads(output.out) == {"sources": [{
        "id": "source-1", "name": "Fixture Jobs", "provider": "greenhouse",
        "enabled": True, "health_status": "HEALTHY",
    }]}
    assert "DO_NOT_PRINT" not in output.out + output.err
    assert "private.example" not in output.out + output.err


@pytest.mark.parametrize("origin", [
    "http://localhost:8000", "http://0.0.0.0:8000", "http://8.8.8.8:8000",
    "https://127.0.0.1:8000", "http://127.0.0.1:8000/path",
    "http://127.0.0.1:8000/?next=https://evil.example",
    "http://username:password@127.0.0.1:8000",
    "http://127.0.0.1:8000/#anchor", "http://127.0.0.1:99999",
    "http://127.0.0.1:8000\\evil.example", "http://[::1%25eth0]:8000",
])
def test_refuses_noncanonical_or_nonloopback_origins(origin, capsys):
    def unexpected(_request):
        raise AssertionError("Invalid origin must not reach transport")

    assert maintenance.main(["--base-url", origin, "health"],
                            transport=_transport(unexpected)) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "CLI_ORIGIN_INVALID" in out.err
    assert "password" not in out.err


def test_accepts_canonical_ipv6_loopback(capsys):
    def handler(request):
        assert request.url.host == "::1"
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "check": "liveness",
            "external_submission_enabled": False, "mode": "LOCAL_FIRST",
            "operation_mode": "PERSONAL"})
    assert maintenance.main(["--base-url", "http://[::1]:4321", "health"],
                            transport=_transport(handler)) == 0
    assert json.loads(capsys.readouterr().out)["operation_mode"] == "PERSONAL"


def test_refuses_redirect_without_following_external_location(capsys):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(302, headers={"location": "https://evil.example/private"})
    assert maintenance.main(["source", "list"], transport=_transport(handler)) == 2
    out = capsys.readouterr()
    assert calls == ["/sources"]
    assert "CLI_REMOTE_UNAVAILABLE" in out.err
    assert "evil.example" not in out.out + out.err


def test_large_response_rejected_without_echoing_body(capsys):
    transport = _transport(lambda _request: httpx.Response(
        200, content=b"PRIVATE" + b"x" * (maintenance.MAX_RESPONSE_BYTES + 1),
        headers={"content-type": "application/json"}))
    assert maintenance.main(["health"], transport=transport) == 2
    out = capsys.readouterr()
    assert "CLI_RESPONSE_TOO_LARGE" in out.err
    assert "PRIVATE" not in out.out + out.err


@pytest.mark.parametrize("payload", [
    {"status": "ok", "check": "liveness", "external_submission_enabled": True},
    {"status": "degraded", "check": "liveness", "external_submission_enabled": False},
    {"status": "ok", "check": "readiness", "external_submission_enabled": False},
])
def test_health_refuses_unexpected_contract(payload, capsys):
    transport = _transport(lambda _request: httpx.Response(200, json=payload))
    assert maintenance.main(["health"], transport=transport) == 2
    assert "CLI_HEALTH_INVALID" in capsys.readouterr().err


def test_malformed_sources_and_json_fail_closed(capsys):
    for response in (httpx.Response(200, json={"sources": "private"}),
                     httpx.Response(200, content=b"not json",
                                    headers={"content-type": "application/json"})):
        assert maintenance.main(["source", "list"], transport=_transport(
            lambda _request, response=response: response)) == 2
        assert capsys.readouterr().out == ""


def test_refuses_compressed_response_before_decoding(capsys):
    import gzip

    payload = {"status": "ok", "check": "liveness", "mode": "LOCAL_FIRST",
               "operation_mode": "FIXTURE", "external_submission_enabled": False}
    compressed = gzip.compress(json.dumps(payload).encode())
    transport = _transport(lambda _request: httpx.Response(
        200, content=compressed,
        headers={"content-type": "application/json", "content-encoding": "gzip"}))
    assert maintenance.main(["health"], transport=transport) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "CLI_REMOTE_UNAVAILABLE" in out.err


def test_transport_failure_does_not_leak_exception(capsys):
    def handler(request):
        raise httpx.ConnectError("private transport exception", request=request)
    assert maintenance.main(["health"], transport=_transport(handler)) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "CLI_REMOTE_UNAVAILABLE" in out.err
    assert "private transport exception" not in out.err


def test_real_owned_loopback_socket_get_and_cleanup(capsys):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.command, self.path))
            body = json.dumps({
                "status": "ok", "check": "liveness", "mode": "LOCAL_FIRST",
                "operation_mode": "FIXTURE", "external_submission_enabled": False,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    try:
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_address[1])
        assert maintenance.main(["--base-url", origin, "health"]) == 0
        assert observed == [("GET", "/health")]
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert not thread.is_alive()
