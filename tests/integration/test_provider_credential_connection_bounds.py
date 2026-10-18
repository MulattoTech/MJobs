"""Actual loopback sockets for fixed-origin credential probes; no live tokens."""
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import tempfile
import threading
import time
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from services.credentials import connection
from tests.integration.test_spine import PROFILE


SECRET = "fictional-credential-probe-token"
PROBE = connection.test_provider_connection


class ProbeServer:
    """Finite fixture watchdog; only this client's request URL is redirected."""

    def __init__(self, mode="body", status=200, duration=2):
        self.started = threading.Event()
        self.closed = threading.Event()
        self.finish = threading.Event()
        self.calls = 0
        self.origins = []
        self.auth_valid = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls += 1
                owner.auth_valid.append(
                    self.headers.get("authorization") == "Bearer " + SECRET
                    or (self.headers.get("x-api-key") == SECRET
                        and self.headers.get("anthropic-version") == "2023-06-01"))
                self.connection.settimeout(.5)
                try:
                    self.wfile.write(f"HTTP/1.1 {status} Fixture\r\n".encode())
                    if mode == "headers":
                        self.wfile.write(b"X-Trickle: ")
                    else:
                        # An intentionally unused, potentially huge response.
                        self.wfile.write(b"Content-Length: 1000000000\r\n"
                                         b"Location: http://127.0.0.1:1/must-not-follow\r\n\r\n")
                    self.wfile.flush()
                    owner.started.set()
                    end = time.monotonic() + duration
                    while time.monotonic() < end and not owner.finish.wait(.02):
                        if select.select([self.connection], [], [], 0)[0]:
                            if self.connection.recv(1) == b"":
                                owner.closed.set()
                                return
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    if mode == "headers":
                        self.wfile.write(b"\r\nContent-Length: 0\r\n\r\n")
                        self.wfile.flush()
                except (OSError, ValueError):
                    owner.closed.set()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01})

    def client_factory(self, **kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"].read <= 8

        def route(request):
            self.origins.append(str(request.url))
            assert str(request.url) in {
                "https://api.openai.com/v1/models", "https://api.anthropic.com/v1/models"}
            assert request.method == "GET"
            request.url = httpx.URL(f"http://127.0.0.1:{self.server.server_port}/models")

        return httpx.Client(event_hooks={"request": [route]}, **kwargs)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.finish.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


def assert_clean(result, server):
    assert SECRET not in repr(result)
    assert server.calls == 1
    assert len(server.origins) == 1
    assert server.auth_valid == [True]
    assert server.closed.wait(.5), "Request socket remained open"
    assert not any(t.name == "mjobs-request-deadline" for t in threading.enumerate())


def test_actual_header_trickle_cannot_extend_default_eight_second_cap():
    # No reduced deadline: this is the original real >8s counterexample.
    # Server watchdog finishes headers after 9.2s even on unfixed source.
    with ProbeServer("headers", duration=9.2) as server:
        start = time.monotonic()
        result = PROBE("openai", SECRET, client_factory=server.client_factory)
        elapsed = time.monotonic() - start
        assert server.started.is_set()
        assert result == {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_TIMEOUT"}
        assert elapsed < 9, "Header trickle extended the total eight-second budget"
        assert_clean(result, server)


def test_unused_trickling_body_is_closed_without_download_for_all_statuses():
    for provider in ("openai", "anthropic"):
        for status, expected in ((200, None), (401, "CREDENTIAL_REJECTED"),
                                 (403, "CREDENTIAL_REJECTED"),
                                 (302, "CREDENTIAL_PROVIDER_UNAVAILABLE"),
                                 (503, "CREDENTIAL_PROVIDER_UNAVAILABLE")):
            with ProbeServer(status=status) as server:
                start = time.monotonic()
                result = PROBE(provider, SECRET, client_factory=server.client_factory)
                assert result == {"status": "CONNECTED" if expected is None else "FAILED",
                                  "code": expected}
                assert time.monotonic() - start < 1
                assert_clean(result, server)


def test_direct_cancellation_interrupts_headers_without_affecting_another_probe():
    with ProbeServer("headers") as slow, ProbeServer() as fast:
        cancelled = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(PROBE, "openai", SECRET, client_factory=slow.client_factory,
                                 cancellation_event=cancelled)
            assert slow.started.wait(1)
            cancelled.set()
            healthy = pool.submit(PROBE, "anthropic", SECRET, client_factory=fast.client_factory)
            assert future.result(timeout=1) == {
                "status": "FAILED", "code": "CREDENTIAL_PROVIDER_CANCELLED"}
            assert healthy.result(timeout=1) == {"status": "CONNECTED", "code": None}
        assert_clean(future.result(), slow)
        assert_clean(healthy.result(), fast)


@contextmanager
def profile_api():
    with tempfile.TemporaryDirectory(prefix="credential-bounds-") as directory:
        root = Path(directory)
        url = f"sqlite:///{(root / 'credentials.db').as_posix()}"
        store = Store(url)
        store.migrate()
        store.bind_workspace_mode("PERSONAL")
        store.save_profile(copy.deepcopy(PROFILE))
        key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
        try:
            with patch.dict("os.environ", {"MJOBS_CREDENTIAL_ENCRYPTION_KEY": key}):
                yield root, url, store
        finally:
            store.engine.dispose()


def api_client(store, root):
    return TestClient(create_app(store, "http://127.0.0.1:8090", root,
                                "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))


def test_actual_socket_probe_outcomes_persist_after_api_and_sqlite_reopen(caplog):
    with profile_api() as (root, url, store):
        for mode, status, expected in (("body", 200, None),
                                       ("body", 401, "CREDENTIAL_REJECTED"),
                                       ("headers", 200, "CREDENTIAL_PROVIDER_TIMEOUT")):
            with api_client(store, root) as client, ProbeServer(mode, status) as server:
                saved = client.put("/profiles/fixture/provider-credentials/openai",
                                   json={"secret": SECRET})
                assert saved.status_code == 200

                def invoke(provider, secret):
                    return PROBE(provider, secret, client_factory=server.client_factory)

                # Reduced budget for this API repetition; the separate socket
                # test above executes the unmodified production eight-second cap.
                with patch.object(connection, "_PROBE_TIMEOUT_SECONDS", .25, create=True), \
                        patch.object(connection, "test_provider_connection", side_effect=invoke):
                    response = client.post("/profiles/fixture/provider-credentials/openai/test")
                assert response.status_code == 200
                result = response.json()
                assert result["code"] == expected
                assert result["status"] == ("CONNECTED" if expected is None else "FAILED")
                assert result["last_tested_at"]
                assert_clean(result, server)
            store.engine.dispose()
            reopened = Store(url)
            try:
                with api_client(reopened, root) as client:
                    metadata = client.get("/profiles/fixture/provider-credentials").json()
                item = metadata["credentials"][0]
                assert item["last_test_status"] == result["status"]
                assert item["last_tested_at"] == result["last_tested_at"]
                assert SECRET not in repr(metadata)
            finally:
                reopened.engine.dispose()
            assert SECRET.encode() not in (root / "credentials.db").read_bytes()
    assert SECRET not in caplog.text
