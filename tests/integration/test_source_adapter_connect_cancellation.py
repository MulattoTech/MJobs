"""MJ-REQ-004: real public-worker connect cancellation for remaining adapters.

Only provider endpoint constants and the operation timeout are fixture seams.
No production transport/check/stream is replaced. Synthetic loopback stores only.
"""
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import json
import os
from pathlib import Path
import select as socket_select
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_source_http_socket_budget import tcp_connect_stall
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation
from services.discovery.sources.registry import source_adapter


ENDPOINTS = {"greenhouse": "HOST", "remoteok": "API_URL", "wwr": "FEED_URL"}
LEVER_TENANTS = ("global:synthetic", "eu:synthetic")


def source_tenant(provider):
    return "synthetic" if provider == "greenhouse" else "default"


@contextmanager
def empty_source(provider):
    state = {"requests": 0}
    engaged = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            if provider == "wwr":
                body, content_type = b"<rss><channel><title>Synthetic empty feed</title></channel></rss>", "application/rss+xml"
            else:
                payload = [] if provider in ("remoteok", "lever") else {"jobs": []} if (
                    urlsplit(self.path).path.endswith("/jobs")) else {"name": "Synthetic empty board"}
                body, content_type = json.dumps(payload).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            engaged.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02},
                              name="mjobs-owned-empty-source", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", engaged, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        if thread.is_alive():
            raise AssertionError("Owned positive source did not stop")


class SourceAdapterConnectCancellationTests(unittest.TestCase):
    def test_injected_transports_keep_control_even_when_falsey(self):
        class FalseyTransport(httpx.MockTransport):
            def __bool__(self):
                return False

        sources = [(provider, source_tenant(provider)) for provider in ENDPOINTS]
        sources += [("lever", tenant) for tenant in LEVER_TENANTS]
        for provider, tenant in sources:
            with self.subTest(provider=provider, tenant=tenant):
                requests = []

                def handler(request):
                    requests.append(request)
                    self.assertEqual(request.url.scheme, "https")
                    self.assertEqual(request.method, "GET")
                    if provider == "wwr":
                        return httpx.Response(200, content=b"<rss><channel/></rss>")
                    payload = [] if provider in ("remoteok", "lever") else {"jobs": []} if (
                        request.url.path.endswith("/jobs")) else {"name": "Synthetic empty board"}
                    if provider == "lever":
                        instance = "api.eu.lever.co" if tenant.startswith("eu:") else "api.lever.co"
                        self.assertEqual(request.url.host, instance)
                        self.assertEqual(request.url.path, "/v0/postings/synthetic")
                        self.assertEqual(request.url.params["mode"], "json")
                    return httpx.Response(200, json=payload)

                transport_module = "greenhouse" if provider == "lever" else provider
                with patch(f"services.discovery.sources.{transport_module}.SourceTransport",
                           side_effect=AssertionError("Injected transport must remain authoritative")):
                    adapter = source_adapter({"provider": provider, "tenant": tenant},
                        transport=FalseyTransport(handler))
                    self.assertEqual(adapter.search().items, ())
                self.assertEqual(len(requests), 2 if provider == "greenhouse" else 1)

    def run_source(self, provider, mode, *, tenant=None):
        tenant = tenant or source_tenant(provider)
        with tempfile.TemporaryDirectory(prefix="mjobs-other-source-tcp-") as directory, ExitStack() as cleanup:
            root = Path(directory)
            url = f"sqlite:///{(root / 'source.db').as_posix()}"
            store = Store(url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            cleanup.callback(store.engine.dispose)  # Release Windows SQLite handles before deleting the directory.
            source = empty_source(provider) if mode == "positive" else tcp_connect_stall()
            original_connect = socket.socket.connect
            original_connect_ex = socket.socket.connect_ex
            errors, outcome = [], []
            trace = {}
            worker_ready, release_worker = threading.Event(), threading.Event()
            deadline_started = threading.Event()
            setup_wait_seconds = 5
            worker = None

            def observe_connect(connection, address):
                if mode != "positive" and address[:2] == state["address"]:
                    state["connects"] += 1
                    state["connecting_socket"] = connection
                    if mode == "deadline":
                        trace["connect_entered_at"] = time.monotonic()
                    engaged.set()
                return original_connect(connection, address)

            def observe_connect_ex(connection, address):
                owned = mode != "positive" and address[:2] == state["address"]
                if owned and mode == "deadline":
                    state["connects"] += 1
                    state["connecting_socket"] = connection
                    trace["connect_entered_at"] = time.monotonic()
                status = original_connect_ex(connection, address)
                if owned:
                    if mode == "deadline":
                        trace["connect_ex_returned_at"] = time.monotonic()
                        trace["connect_ex_status"] = status
                        trace["engaged_at"] = trace["connect_ex_returned_at"]
                    else:
                        state["connects"] += 1
                        state["connecting_socket"] = connection
                    engaged.set()
                return status

            def bounded_timeout():
                trace["deadline_started_at"] = time.monotonic()
                trace["operation_deadline_at"] = trace["deadline_started_at"] + .8
                deadline_started.set()
                return .8 if mode == "deadline" else 30

            def diagnostic():
                return (f"trace={trace!r}; worker_errors={errors!r}; "
                        f"connects={state['connects']}; requests={state['requests']}; "
                        f"socket_closed={getattr(state.get('connecting_socket'), 'fileno', lambda: None)() == -1}")

            worker_timeout_patch = (patch("workers.tasks.timeout", new=bounded_timeout)
                                    if mode == "deadline" else patch("workers.tasks.timeout", return_value=30))
            with source as (endpoint, engaged, state), ExitStack() as endpoints, patch.dict(os.environ, {
                    "MJOBS_DATABASE_URL": url, "MJOBS_OPERATION_MODE": "FIXTURE",
                    "MJOBS_OUTPUT_DIR": directory, "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            }), patch("socket.socket.connect", new=observe_connect), patch(
                    "socket.socket.connect_ex", new=observe_connect_ex), worker_timeout_patch:
                if provider == "lever":
                    from services.discovery.sources.lever import endpoint as production_endpoint
                    # Resolve the real regional endpoint first; only replace its origin.
                    path = urlsplit(production_endpoint(tenant)).path
                    def owned_endpoint(actual_tenant):
                        self.assertEqual(actual_tenant, tenant)
                        return endpoint + path
                    endpoints.enter_context(patch("services.discovery.sources.lever.endpoint",
                                                  side_effect=owned_endpoint))
                else:
                    endpoints.enter_context(patch(
                        f"services.discovery.sources.{provider}.{ENDPOINTS[provider]}", endpoint))
                with TestClient(create_app(store, "http://127.0.0.1:1", root,
                        "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
                    self.assertEqual(client.put("/profiles", json=PROFILE).status_code, 200)
                    response = client.put("/sources", json={"provider": provider,
                                          "tenant": tenant,
                                          "request_timeout": 2, "rate_limit": 600})
                    self.assertEqual(response.status_code, 200, response.text)
                    source_id = response.json()["id"]
                    response = client.post(f"/sources/{source_id}/collect", json={
                        "profile_id": PROFILE["id"], "request_id": uuid4().hex})
                    self.assertEqual(response.status_code, 202, response.text)
                    identifier = response.json()["operation_id"]

                    def execute():
                        if mode == "deadline":
                            worker_ready.set()
                            if not release_worker.wait(setup_wait_seconds):
                                errors.append("WorkerReleaseTimeout")
                                return
                            trace["worker_started_at"] = time.monotonic()
                        try:
                            outcome.append(execute_operation(identifier))
                        except BaseException as error:
                            errors.append(f"{type(error).__name__}: {error}")

                    worker = threading.Thread(target=execute, name="mjobs-owned-adapter-worker", daemon=True)
                    worker_created_at = time.monotonic()
                    worker.start()
                    try:
                        if mode == "deadline":
                            ready = worker_ready.wait(setup_wait_seconds)
                            trace["worker_ready_at"] = time.monotonic() if ready else None
                            trace["engaged_before_release"] = engaged.is_set()
                            release_worker.set()
                            deadline_observed = deadline_started.wait(setup_wait_seconds)
                            trace["deadline_observed_at"] = time.monotonic() if deadline_observed else None
                            engagement_deadline_at = trace["deadline_started_at"] + 1.0 if deadline_observed else None
                            trace["engagement_deadline_at"] = engagement_deadline_at
                            engagement_window = max(0.0, engagement_deadline_at - time.monotonic()) if deadline_observed else 0.0
                            boundary_reached = engaged.wait(engagement_window)
                            trace["boundary_observed_at"] = time.monotonic() if boundary_reached else None
                            if boundary_reached:
                                connection = state["connecting_socket"]
                                trace["pending_socket_samples"] = []
                                while time.monotonic() < trace["operation_deadline_at"]:
                                    was_open = connection.fileno() >= 0
                                    writable = None
                                    if was_open:
                                        try:
                                            writable = bool(socket_select.select([], [connection], [], 0)[1])
                                        except (OSError, ValueError):
                                            pass
                                    is_open = connection.fileno() >= 0
                                    trace["pending_socket_samples"].append({
                                        "at": time.monotonic(),
                                        "open": was_open and is_open,
                                        "writable": writable,
                                    })
                                    remaining = trace["operation_deadline_at"] - time.monotonic()
                                    if remaining <= 0 or not is_open:
                                        break
                                    time.sleep(min(.01, remaining))
                                trace["pending_socket_observation_ended_at"] = time.monotonic()
                        else:
                            self.assertTrue(engaged.wait(1), f"{provider} never reached owned TCP boundary")
                        pending = None
                        if mode == "cancel":
                            connection = state["connecting_socket"]
                            self.assertGreaterEqual(connection.fileno(), 0)
                            self.assertFalse(socket_select.select([], [connection], [], 0)[1],
                                             "Connect must still be pending at public cancellation")
                            started = time.monotonic()
                            response = client.post(f"/operations/{identifier}/cancel")
                            self.assertEqual(response.status_code, 202, response.text)
                            worker.join(timeout=.35)
                            if worker.is_alive():
                                pending = client.get(f"/operations/{identifier}").json()
                                worker.join(timeout=2.3)  # Observe natural timeout, never force green by closing the listener.
                        else:
                            started = worker_created_at
                            worker.join(timeout=3 if mode == "positive" else 1.1)
                        elapsed = time.monotonic() - started
                        self.assertFalse(worker.is_alive(), f"Owned {provider} worker exceeded fixture bound")
                        trace["worker_finished_at"] = time.monotonic()
                        self.assertEqual(errors, [], diagnostic())
                        self.assertEqual(len(outcome), 1)
                        expected = "COMPLETED" if mode == "positive" else "CANCELLED" if mode == "cancel" else "DEAD_LETTERED"
                        reopened = Store(url)
                        try:
                            with TestClient(create_app(reopened, "http://127.0.0.1:1", root,
                                    "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as fresh:
                                saved = fresh.get(f"/operations/{identifier}").json()
                                trace["terminal_readback_at"] = time.monotonic()
                                self.assertEqual(saved["state"], expected, saved)
                                self.assertEqual(saved["attempt_count"], 1)
                                if mode != "positive":
                                    self.assertEqual(saved["error"], "CANCEL_REQUESTED" if mode == "cancel" else "OPERATION_TIMEOUT")
                                public_source = next(row for row in fresh.get("/sources").json()["sources"]
                                                     if row["id"] == source_id)
                                self.assertEqual(public_source["provider"], provider)
                                self.assertEqual(public_source["tenant"], tenant)
                                self.assertEqual(public_source["health_status"],
                                                 "HEALTHY" if mode == "positive" else "NOT_CHECKED")
                            with reopened.engine.connect() as connection:
                                health = connection.execute(select(m.job_sources).where(
                                    m.job_sources.c.id == source_id)).mappings().one()
                            self.assertEqual(health["health_status"], "HEALTHY" if mode == "positive" else "NOT_CHECKED")
                            self.assertEqual(health["consecutive_failures"], 0)
                            self.assertIsNone(health["last_error"])
                            self.assertEqual(reopened.count("canonical_jobs"), 0)
                        finally:
                            reopened.engine.dispose()
                        self.assertEqual(state["requests"], (2 if provider == "greenhouse" else 1) if mode == "positive" else 0)
                        if mode != "positive":
                            self.assertEqual(state["connects"], 1, diagnostic())
                            self.assertEqual(state["connecting_socket"].fileno(), -1, diagnostic())
                        if mode == "deadline":
                            self.assertFalse(trace["engaged_before_release"],
                                             f"{provider} reached connect before worker release; {diagnostic()}")
                            self.assertTrue(ready, f"{provider} worker did not reach release gate; {diagnostic()}")
                            self.assertTrue(deadline_observed,
                                            f"{provider} operation did not establish its deadline; {diagnostic()}")
                            self.assertTrue(boundary_reached,
                                            f"{provider} did not reach the owned connect attempt within 1s of the operation deadline; {diagnostic()}")
                            self.assertNotIn(trace["connect_ex_status"], (0, errno.EISCONN),
                                             f"{provider} connect_ex completed instead of remaining pending; {diagnostic()}")
                            samples = [sample for sample in trace.get("pending_socket_samples", [])
                                       if sample["at"] <= trace["operation_deadline_at"]]
                            self.assertGreaterEqual(len(samples), 2,
                                                    f"{provider} socket was not observed through the operation deadline; {diagnostic()}")
                            self.assertTrue(all(sample["open"] and sample["writable"] is False for sample in samples),
                                            f"{provider} socket did not remain open and non-writable through the operation deadline; {diagnostic()}")
                            self.assertGreaterEqual(trace["pending_socket_observation_ended_at"],
                                                    trace["operation_deadline_at"],
                                                    f"{provider} socket observation ended before the .8s operation deadline; {diagnostic()}")
                            self.assertLessEqual(trace["connect_entered_at"], trace["engagement_deadline_at"],
                                                 f"{provider} connect attempt missed the 1s engagement deadline; {diagnostic()}")
                        print(f"OWNED_ADAPTER_CONNECT provider={provider} tenant={tenant} mode={mode} latency={elapsed:.3f}s")
                        if mode == "cancel":
                            self.assertLess(elapsed, .35, f"{provider}: public cancel blocked {elapsed:.3f}s; "
                                f"pending={pending and (pending['state'], pending['cancel_requested'])}; "
                                f"durable={saved['state']}/{saved['error']}; HTTP={state['requests']}")
                    finally:
                        worker.join(timeout=3)
                        self.assertFalse(worker.is_alive(), "Owned adapter worker leaked")

    def test_greenhouse_public_cancel_during_real_tcp_connect(self):
        self.run_source("greenhouse", "cancel")

    def test_remoteok_public_cancel_during_real_tcp_connect(self):
        self.run_source("remoteok", "cancel")

    def test_wwr_public_cancel_during_real_tcp_connect(self):
        self.run_source("wwr", "cancel")

    def test_positive_public_worker_collections_and_fresh_readback(self):
        for provider in ENDPOINTS:
            with self.subTest(provider=provider):
                self.run_source(provider, "positive")

    def test_finite_worker_deadline_during_real_tcp_connect(self):
        for provider in ENDPOINTS:
            with self.subTest(provider=provider):
                self.run_source(provider, "deadline")

    def test_lever_global_public_cancel_during_real_tcp_connect(self):
        self.run_source("lever", "cancel", tenant="global:synthetic")

    def test_lever_eu_public_cancel_during_real_tcp_connect(self):
        self.run_source("lever", "cancel", tenant="eu:synthetic")

    def test_lever_global_finite_deadline_during_real_tcp_connect(self):
        self.run_source("lever", "deadline", tenant="global:synthetic")

    def test_lever_eu_finite_deadline_during_real_tcp_connect(self):
        self.run_source("lever", "deadline", tenant="eu:synthetic")

    def test_lever_global_healthy_worker_and_fresh_source_readback(self):
        self.run_source("lever", "positive", tenant="global:synthetic")

    def test_lever_eu_healthy_worker_and_fresh_source_readback(self):
        self.run_source("lever", "positive", tenant="eu:synthetic")
