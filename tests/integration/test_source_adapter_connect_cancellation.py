"""MJ-REQ-004: real public-worker connect cancellation for remaining adapters.

Provider endpoints, operation timeout and SSL-context readiness are fixture seams.
Only cold-SSL accounting uses an owned operation clock; socket/cancellation
budgets retain the real clock. No production transport/check/stream is replaced.
Synthetic loopback stores only.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select as socket_select
import socket
import ssl
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import httpx._transports.default as httpx_default
from sqlalchemy import select

from apps.api import main as api_main
from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_source_http_socket_budget import tcp_connect_stall
from tests.integration.test_spine import PROFILE
from workers import tasks as worker_tasks
from workers.tasks import execute_operation
from services.discovery.sources import http_transport
from services.discovery.sources.registry import source_adapter


ENDPOINTS = {"greenhouse": "HOST", "remoteok": "API_URL", "wwr": "FEED_URL"}
LEVER_TENANTS = ("global:synthetic", "eu:synthetic")


def source_tenant(provider):
    return "synthetic" if provider == "greenhouse" else "default"


class OwnedRequestThreads:
    """Include only the owned worker and its real Greenhouse request pool."""

    def __init__(self, worker):
        self.worker = worker
        self._pool = threading.local()

    def owns_current_thread(self):
        return threading.current_thread() is self.worker or getattr(self._pool, "owned", False)

    def executor(self, max_workers=None, thread_name_prefix="", initializer=None, initargs=()):
        if (threading.current_thread() is not self.worker
                or (initializer is not None and not callable(initializer))):
            return ThreadPoolExecutor(max_workers, thread_name_prefix, initializer, initargs)

        def initialize():
            self._pool.owned = True
            if initializer is not None:
                initializer(*initargs)

        return ThreadPoolExecutor(max_workers, thread_name_prefix, initialize)


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
    def test_ssl_readiness_owns_only_worker_created_request_threads(self):
        scope = OwnedRequestThreads(threading.current_thread())
        self.assertTrue(scope.owns_current_thread())
        initialized = []

        def initialize(marker):
            initialized.append((threading.current_thread(), marker))

        with scope.executor(1, "mjobs-greenhouse", initialize, ("owned",)) as executor:
            self.assertIsInstance(executor, ThreadPoolExecutor)
            thread, owned = executor.submit(
                lambda: (threading.current_thread(), scope.owns_current_thread())).result(timeout=3)
        self.assertTrue(owned)
        self.assertIsNot(thread, scope.worker)
        self.assertEqual(initialized, [(thread, "owned")])

        def unrelated_pool():
            self.assertFalse(scope.owns_current_thread())
            with scope.executor(1, "mjobs-greenhouse", initialize, ("unrelated",)) as executor:
                return executor.submit(
                    lambda: (threading.current_thread(), scope.owns_current_thread())).result(timeout=3)

        # Identical pool names do not establish fixture ownership. An unrelated
        # caller must retain its real initializer and remain outside this scope.
        with ThreadPoolExecutor(1, "mjobs-greenhouse") as unrelated:
            thread, owned = unrelated.submit(unrelated_pool).result(timeout=3)
        self.assertFalse(owned)
        self.assertEqual(initialized[-1], (thread, "unrelated"))

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

    def run_source(self, provider, mode, *, tenant=None, coordinator_delay=0,
                   transport_setup_delay=0, worker_unwind_delay=0, ssl_setup_delay=0,
                   cancel_dispatch_delay=0, cancel_service_delay=0):
        tenant = tenant or source_tenant(provider)
        cold_ssl = mode == "cold_ssl_deadline"
        pre_ssl_deadline = mode == "pre_ssl_deadline"
        unprepared_ssl = cold_ssl or pre_ssl_deadline
        finite_deadline = mode == "deadline" or unprepared_ssl
        with tempfile.TemporaryDirectory(prefix="mjobs-other-source-tcp-") as directory, ExitStack() as cleanup:
            root = Path(directory)
            url = f"sqlite:///{(root / 'source.db').as_posix()}"
            store = Store(url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            cleanup.callback(store.engine.dispose)  # Release Windows SQLite handles before deleting the directory.
            source = empty_source(provider) if mode == "positive" else tcp_connect_stall()
            original_collect_source = worker_tasks.collect_source
            original_connect = socket.socket.connect
            original_connect_ex = socket.socket.connect_ex
            original_connect_tcp = http_transport._Backend.connect_tcp
            errors, outcome = [], []
            worker_ready = threading.Event()
            worker_ready_at = []
            collection_ready = threading.Event()
            collection_release = threading.Event()
            deadline_guard_armed = threading.Event()
            deadline_guard_release = threading.Event()
            deadline_guard_released = threading.Event()
            deadline_started_at = []
            transport_entered = threading.Event()
            transport_completed = threading.Event()
            transport_operation_completed = threading.Event()
            phases = {}
            # Only the cold-SSL accounting case uses a controlled operation
            # clock. The production deadline is still created once at worker
            # entry; its clock advances only inside the real SSL factory below.
            cold_clock_value = [0.0]
            cold_clock = SimpleNamespace(monotonic=lambda: cold_clock_value[0]) if cold_ssl else None
            ssl_factory_calls = {"entered": 0, "completed": 0}
            cancel_calls = []
            worker_finished_at = []
            worker = None
            request_threads = None

            # Transport-ready cases prepare the trust store separately. The
            # cold-context case charges real factory completion to its owned
            # operation clock. The separate pre-SSL case keeps the real clock.
            # Keep the real verifying context and real
            # SourceTransport/backend; only its construction is made ready before
            # the unchanged operation-start clock. Never cache a transport/check.
            original_ssl_context = httpx_default.create_ssl_context
            if not unprepared_ssl:
                ssl_setup_started_at = time.monotonic()
                if ssl_setup_delay:
                    time.sleep(ssl_setup_delay)
                ssl_context = original_ssl_context(verify=True, cert=None, trust_env=False)
                ssl_context_ready_at = time.monotonic()
                self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(ssl_context.check_hostname)
                self.assertGreaterEqual(ssl_context_ready_at - ssl_setup_started_at, ssl_setup_delay)
            ssl_context_uses = []
            ssl_context_threads = []

            def ready_ssl_context(verify=True, cert=None, trust_env=True):
                if (request_threads is not None and request_threads.owns_current_thread() and verify is True
                        and cert is None and trust_env is False):
                    if unprepared_ssl:
                        ssl_factory_calls["entered"] += 1
                        phases["cold_ssl_wall_started_at"] = time.monotonic()
                        phases["cold_ssl_started_at"] = (
                            cold_clock.monotonic() if cold_ssl else time.monotonic())
                        if not cold_ssl:
                            time.sleep(ssl_setup_delay)
                        context = original_ssl_context(verify=verify, cert=cert, trust_env=trust_env)
                        if cold_ssl:
                            # Model setup cost without timing unrelated fixture
                            # scheduling. Never move/reset the existing deadline
                            # or replace its production cancellation checks.
                            cold_clock_value[0] += ssl_setup_delay
                        phases["cold_ssl_completed_at"] = (
                            cold_clock.monotonic() if cold_ssl else time.monotonic())
                        phases["cold_ssl_wall_completed_at"] = time.monotonic()
                        ssl_factory_calls["completed"] += 1
                        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                        self.assertTrue(context.check_hostname)
                        return context
                    ssl_context_uses.append(time.monotonic())
                    ssl_context_threads.append(threading.current_thread())
                    return ssl_context
                return original_ssl_context(verify=verify, cert=cert, trust_env=trust_env)

            def phase_diagnostic():
                deadline_start = deadline_started_at[0] if deadline_started_at else None
                timestamp_keys = (
                    "worker_ready_at", "worker_release_signaled_at", "worker_released_at",
                    "deadline_guard_armed_at", "deadline_guard_release_signaled_at",
                    "deadline_guard_released_at", "operation_deadline_started_at",
                    "collector_entered_at", "transport_call_entered_at",
                    "transport_call_completed_at", "transport_operation_completed_at",
                    "worker_outcome_at", "worker_error_at",
                    "worker_finished_at", "terminal_readback_at",
                    "cold_ssl_started_at", "cold_ssl_completed_at",
                    "cold_ssl_wall_started_at", "cold_ssl_wall_completed_at",
                )
                timestamps = {}
                for key in timestamp_keys:
                    value = phases.get(key)
                    if isinstance(value, (int, float)):
                        timestamps[key] = value
                if deadline_start is not None:
                    # Do not subtract a controlled operation timestamp from a
                    # wall-clock observation. Wall phases remain in phases below.
                    operation_keys = {"operation_deadline_started_at",
                                      "cold_ssl_started_at", "cold_ssl_completed_at"}
                    relative_ms = {
                        key: round((value - deadline_start) * 1000, 1)
                        for key, value in timestamps.items()
                        if not cold_ssl or key in operation_keys
                    }
                else:
                    relative_ms = {}
                connection = state.get("connecting_socket")
                return (f"phase_ms_from_deadline={relative_ms}; "
                        f"deadline_clock={'controlled' if cold_ssl else 'real'}; "
                        f"ssl_factory_calls={ssl_factory_calls!r}; phases={phases!r}; "
                        f"worker_errors={errors!r}; outcomes={len(outcome)}; "
                        f"connects={state.get('connects', 0)}; requests={state.get('requests', 0)}; "
                        f"socket_fd={connection.fileno() if connection is not None else None}")

            def observe_collect_source(*args, **kwargs):
                # This is a sticky, non-blocking observation after the production
                # deadline has started. Never wait here: scheduler delay belongs
                # to the test coordinator, not the production worker's deadline.
                state["collection_entered_at"] = time.monotonic()
                phases["collector_entered_at"] = state["collection_entered_at"]
                collection_ready.set()
                if transport_setup_delay:
                    # Model a loaded shard scheduling the real provider setup
                    # after collector entry. Cancellation latency starts only
                    # after the owned pending socket is observed below.
                    time.sleep(transport_setup_delay)
                try:
                    return original_collect_source(*args, **kwargs)
                finally:
                    # Preserve a deterministic distinction between transport
                    # interruption and later worker bookkeeping/persistence.
                    if worker_unwind_delay:
                        time.sleep(worker_unwind_delay)

            def mark_transport_entry(connection, address, operation):
                if mode != "positive" and address[:2] == state["address"]:
                    state["connects"] += 1
                    state["connecting_socket"] = connection
                    phases["transport_call"] = operation
                    phases["transport_call_entered_at"] = time.monotonic()
                    state["connect_entered_at"] = phases["transport_call_entered_at"]
                    transport_entered.set()
                    return True
                return False

            def observe_connect(connection, address):
                owned = mark_transport_entry(connection, address, "connect")
                try:
                    return original_connect(connection, address)
                finally:
                    if owned:
                        phases["transport_call_completed_at"] = time.monotonic()
                        transport_completed.set()
                        engaged.set()

            def observe_connect_ex(connection, address):
                owned = mark_transport_entry(connection, address, "connect_ex")
                try:
                    status = original_connect_ex(connection, address)
                    if owned:
                        phases["connect_ex_status"] = status
                    return status
                except BaseException as error:
                    if owned:
                        phases["transport_call_error"] = f"{type(error).__name__}: {error}"
                    raise
                finally:
                    if owned:
                        phases["transport_call_completed_at"] = time.monotonic()
                        transport_completed.set()
                        engaged.set()

            def observe_connect_tcp(backend, *args, **kwargs):
                try:
                    return original_connect_tcp(backend, *args, **kwargs)
                finally:
                    phases["transport_operation_completed_at"] = time.monotonic()
                    transport_operation_completed.set()

            class GuardedDeadline:
                def __radd__(self, _sampled_at):
                    phases["deadline_guard_armed_at"] = time.monotonic()
                    deadline_guard_armed.set()
                    if not deadline_guard_release.wait(5):
                        raise AssertionError("Test did not release armed operation deadline")
                    phases["deadline_guard_released_at"] = time.monotonic()
                    # The coordinator delay is fixture setup, not part of the
                    # strict real-transport deadline. Start the unchanged .8s
                    # budget only after both sides cross the causal gate.
                    started_at = cold_clock.monotonic() if cold_ssl else time.monotonic()
                    deadline_started_at.append(started_at)
                    phases["operation_deadline_started_at"] = started_at
                    deadline_guard_released.set()
                    return started_at + .8

            def record_timeout():
                return GuardedDeadline() if finite_deadline else 30

            original_request_cancel = api_main.request_cancel

            def observe_request_cancel(*args, **kwargs):
                call = {"entered_at": time.monotonic()}
                cancel_calls.append(call)
                connection = state["connecting_socket"]
                call["socket_pending_at_entry"] = (connection.fileno() >= 0
                    and not socket_select.select([], [connection], [], 0)[1])
                try:
                    if cancel_service_delay:
                        # Negative control: delay inside the measured service
                        # boundary must still violate the cancellation budget.
                        time.sleep(cancel_service_delay)
                    return original_request_cancel(*args, **kwargs)
                finally:
                    call["exited_at"] = time.monotonic()

            with source as (endpoint, engaged, state), ExitStack() as endpoints, patch.dict(os.environ, {
                    "MJOBS_DATABASE_URL": url, "MJOBS_OPERATION_MODE": "FIXTURE",
                    "MJOBS_OUTPUT_DIR": directory, "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            }), patch("socket.socket.connect", new=observe_connect), patch(
                    "socket.socket.connect_ex", new=observe_connect_ex), patch(
                    "services.discovery.sources.http_transport._Backend.connect_tcp",
                    new=observe_connect_tcp), patch(
                    "workers.tasks.collect_source", new=observe_collect_source), patch(
                    "workers.tasks.timeout", side_effect=record_timeout), patch(
                    "httpx._transports.default.create_ssl_context", new=ready_ssl_context):
                if cold_ssl:
                    # Replace this module binding, never attributes on the
                    # process-wide time module used by other timing fixtures.
                    endpoints.enter_context(patch.object(worker_tasks, "time", cold_clock))
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
                if mode == "cancel":
                    endpoints.enter_context(patch("apps.api.main.request_cancel",
                                                  new=observe_request_cancel))
                app = create_app(store, "http://127.0.0.1:1", root,
                                 "redis://127.0.0.1:1/0", operation_mode="FIXTURE")

                async def dispatch(scope, receive, send):
                    if (cancel_dispatch_delay and scope["type"] == "http"
                            and scope["method"] == "POST" and scope["path"].endswith("/cancel")):
                        # Exercise slow fixture/ASGI scheduling before the real
                        # synchronous cancellation service is admitted.
                        # Use the measured clock as the oracle: a timer callback
                        # can be scheduled slightly before its requested delay.
                        dispatch_ready_at = time.monotonic() + cancel_dispatch_delay
                        while (remaining := dispatch_ready_at - time.monotonic()) > 0:
                            await asyncio.sleep(remaining)
                    await app(scope, receive, send)

                with TestClient(dispatch) as client:
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
                        try:
                            # This gate is deliberately before execute_operation,
                            # where workers.tasks creates its deadline.
                            ready_at = time.monotonic()
                            worker_ready_at.append(ready_at)
                            phases["worker_ready_at"] = ready_at
                            worker_ready.set()
                            if not collection_release.wait(5):
                                raise AssertionError("Test did not release worker before operation entry")
                            phases["worker_released_at"] = time.monotonic()
                            result = execute_operation(identifier)
                            outcome.append(result)
                            phases["worker_result_status"] = (result.get("status")
                                                               if isinstance(result, dict)
                                                               else type(result).__name__)
                            phases["worker_outcome_at"] = time.monotonic()
                        except BaseException as error:
                            errors.append(f"{type(error).__name__}: {error}")
                            phases["worker_error_at"] = time.monotonic()
                        finally:
                            finished_at = time.monotonic()
                            worker_finished_at.append(finished_at)
                            phases["worker_finished_at"] = finished_at

                    worker = threading.Thread(target=execute, name="mjobs-owned-adapter-worker", daemon=True)
                    request_threads = OwnedRequestThreads(worker)
                    if provider == "greenhouse":
                        endpoints.enter_context(patch(
                            "services.discovery.sources.greenhouse.ThreadPoolExecutor",
                            new=request_threads.executor))
                    started = time.monotonic()
                    worker.start()
                    try:
                        self.assertTrue(worker_ready.wait(3),
                            f"{provider} worker never reached pre-deadline gate; "
                            f"{phase_diagnostic()}")
                        phases["worker_release_signaled_at"] = time.monotonic()
                        collection_release.set()
                        if finite_deadline:
                            self.assertTrue(deadline_guard_armed.wait(3),
                                f"{provider} worker never armed its operation deadline; "
                                f"{phase_diagnostic()}")
                            if coordinator_delay:
                                time.sleep(coordinator_delay)
                            phases["deadline_guard_release_signaled_at"] = time.monotonic()
                            deadline_guard_release.set()
                            self.assertTrue(deadline_guard_released.wait(3),
                                f"{provider} worker never released its operation deadline; "
                                f"{phase_diagnostic()}")
                        self.assertTrue(collection_ready.wait(3),
                            f"{provider} worker never reached source collection; "
                            f"{phase_diagnostic()}")
                        if mode == "deadline":
                            self.assertEqual(len(deadline_started_at), 1,
                                f"{provider} source collector entered without one recorded operation deadline; "
                                f"{phase_diagnostic()}")
                            self.assertLessEqual(ssl_context_ready_at, deadline_started_at[0],
                                "Trust-store initialization must finish before the operation deadline starts")
                            self.assertLessEqual(phases["worker_released_at"],
                                                 phases["deadline_guard_armed_at"],
                                f"{provider} deadline guard armed before worker release; "
                                f"{phase_diagnostic()}")
                            self.assertLessEqual(phases["deadline_guard_release_signaled_at"],
                                                 phases["deadline_guard_released_at"],
                                f"{provider} deadline guard escaped before coordinator release; "
                                f"{phase_diagnostic()}")
                            self.assertLessEqual(phases["deadline_guard_released_at"],
                                                 deadline_started_at[0],
                                f"{provider} operation deadline preceded guard release; "
                                f"{phase_diagnostic()}")
                            self.assertLessEqual(deadline_started_at[0], phases["collector_entered_at"],
                                f"{provider} source collector entry preceded operation-deadline construction; "
                                f"{phase_diagnostic()}")
                            self.assertTrue(engaged.wait(1),
                                f"{provider} did not complete one owned TCP connect attempt; "
                                f"{phase_diagnostic()}")
                            connect_window_end = deadline_started_at[0] + 1.0
                            entry_wait = max(0, connect_window_end - time.monotonic())
                            entry_observed = transport_entered.wait(entry_wait)
                            self.assertTrue(entry_observed,
                                f"{provider} reached the collector but no owned transport connect call was entered "
                                f"within 1s of the operation-deadline start; {phase_diagnostic()}")
                            self.assertLessEqual(phases["transport_call_entered_at"] - deadline_started_at[0], 1.0,
                                f"{provider} transport connect entry exceeded the 1s operation-start window; "
                                f"{phase_diagnostic()}")
                            self.assertLessEqual(phases["collector_entered_at"],
                                                 phases["transport_call_entered_at"],
                                f"{provider} transport connect entry preceded source-collector readiness; "
                                f"{phase_diagnostic()}")
                            completion_wait = max(0, connect_window_end - time.monotonic())
                            completion_observed = transport_completed.wait(completion_wait)
                            self.assertTrue(completion_observed,
                                f"{provider} entered the transport connect call but it did not return within "
                                f"the same 1s window; {phase_diagnostic()}")
                            self.assertLessEqual(phases["transport_call_completed_at"] - deadline_started_at[0], 1.0,
                                f"{provider} transport connect call completed after the 1s operation-start window; "
                                f"{phase_diagnostic()}")
                        elif mode == "cancel":
                            # Coordinate cancellation from entry to the real
                            # owned connect call. The older `engaged` event is a
                            # connect-completion observation and can invert this
                            # ordering on a genuinely pending socket.
                            self.assertTrue(transport_entered.wait(3),
                                f"{provider} did not enter an owned TCP boundary; {phase_diagnostic()}")
                            self.assertLessEqual(phases["collector_entered_at"],
                                                 phases["transport_call_entered_at"],
                                f"{provider} transport entry preceded collector readiness; "
                                f"{phase_diagnostic()}")
                        if coordinator_delay:
                            self.assertGreater(coordinator_delay, .8)
                            self.assertEqual(len(deadline_started_at), 1)
                            self.assertGreater(deadline_started_at[0] - worker_ready_at[0], .8,
                                "Forced coordinator delay must precede deadline construction")
                            self.assertLess(state["collection_entered_at"] - deadline_started_at[0], .8,
                                "Source collection entry must occur within the new deadline")
                            self.assertLess(state["connect_entered_at"] - deadline_started_at[0], .8,
                                "TCP connect entry must not inherit coordinator scheduling delay")
                        pending = None
                        if mode == "cancel":
                            connection = state["connecting_socket"]
                            self.assertGreaterEqual(connection.fileno(), 0)
                            self.assertFalse(socket_select.select([], [connection], [], 0)[1],
                                             "Connect must still be pending at public cancellation")
                            started = time.monotonic()
                            response = client.post(f"/operations/{identifier}/cancel")
                            response_finished_at = time.monotonic()
                            cancel_response_elapsed = response_finished_at - started
                            self.assertEqual(response.status_code, 202, response.text)
                            self.assertEqual(len(cancel_calls), 1,
                                             f"Expected one public cancellation call, got {len(cancel_calls)}")
                            cancel_call = cancel_calls[0]
                            # Keep one immutable admission timestamp, including
                            # durable cancellation in both strict .35s oracles.
                            # TestClient/AnyIO dispatch is separately bounded as
                            # fixture readiness, not claimed as HTTP latency.
                            cancel_started_at = cancel_call["entered_at"]
                            cancel_dispatch_elapsed = cancel_started_at - started
                            cancel_service_response_elapsed = response_finished_at - cancel_started_at
                            cancel_store_elapsed = cancel_call["exited_at"] - cancel_started_at
                            cancel_response_tail_elapsed = response_finished_at - cancel_call["exited_at"]
                            self.assertTrue(cancel_call["socket_pending_at_entry"],
                                            "Owned connect must still be pending at cancellation service entry")
                            self.assertLess(cancel_dispatch_elapsed, 3,
                                f"{provider}: cancellation fixture dispatch exceeded readiness bound; "
                                f"dispatch={cancel_dispatch_elapsed:.3f}s")
                            self.assertGreaterEqual(cancel_dispatch_elapsed, cancel_dispatch_delay)
                            worker.join(timeout=.35)
                            if worker.is_alive():
                                pending = client.get(f"/operations/{identifier}").json()
                                worker.join(timeout=2.3)  # Observe natural timeout, never force green by closing the listener.
                        else:
                            # Deadline mode already proves the owned transport entered and
                            # returned within one second of the operation-deadline start.
                            # Keep persistence/unwind scheduling separate from that network
                            # oracle so a loaded shard cannot turn a terminal operation into
                            # a false worker leak.
                            worker.join(timeout=3)
                        elapsed = time.monotonic() - started
                        self.assertFalse(worker.is_alive(),
                            f"Owned {provider} worker exceeded fixture bound; {phase_diagnostic()}")
                        if mode == "cancel":
                            self.assertEqual(len(worker_finished_at), 1,
                                             "Owned adapter worker completion was not observed exactly once")
                            self.assertTrue(transport_operation_completed.is_set(),
                                            "Owned pending transport completion was not observed")
                            transport_settle_elapsed = phases["transport_operation_completed_at"] - cancel_started_at
                            worker_settle_elapsed = worker_finished_at[0] - cancel_started_at
                            self.assertGreaterEqual(transport_settle_elapsed, 0,
                                "Owned transport must not complete before cancellation service entry")
                            worker_after_response_elapsed = max(
                                0, worker_finished_at[0] - response_finished_at)
                        self.assertEqual(errors, [])
                        self.assertEqual(len(outcome), 1)
                        if unprepared_ssl:
                            self.assertEqual(len(deadline_started_at), 1, phase_diagnostic())
                            self.assertEqual(ssl_context_uses, [],
                                             f"Unprepared setup must not use a prebuilt context; {phase_diagnostic()}")
                            if cold_ssl:
                                self.assertEqual(ssl_factory_calls, {"entered": 1, "completed": 1},
                                    f"Cold SSL must execute the real factory exactly once; {phase_diagnostic()}")
                                self.assertGreater(ssl_setup_delay, .8)
                                self.assertGreaterEqual(phases["cold_ssl_started_at"], deadline_started_at[0],
                                                        phase_diagnostic())
                                self.assertGreaterEqual(
                                    phases["cold_ssl_completed_at"] - phases["cold_ssl_started_at"], ssl_setup_delay,
                                    f"Cold SSL construction must consume its operation-clock budget; {phase_diagnostic()}")
                                self.assertGreater(phases["cold_ssl_completed_at"] - deadline_started_at[0], .8,
                                                   phase_diagnostic())
                                self.assertGreaterEqual(phases["cold_ssl_wall_completed_at"],
                                                        phases["cold_ssl_wall_started_at"], phase_diagnostic())
                            else:
                                self.assertGreater(transport_setup_delay, .8)
                                self.assertEqual(ssl_factory_calls, {"entered": 0, "completed": 0},
                                    f"Pre-SSL timeout must precede the real factory; {phase_diagnostic()}")
                                self.assertNotIn("cold_ssl_started_at", phases, phase_diagnostic())
                                self.assertNotIn("cold_ssl_completed_at", phases, phase_diagnostic())
                                self.assertGreaterEqual(phases["worker_outcome_at"] - deadline_started_at[0],
                                                        transport_setup_delay, phase_diagnostic())
                            self.assertFalse(transport_entered.is_set(),
                                             f"Expired setup must not start a socket connect; {phase_diagnostic()}")
                            self.assertFalse(transport_completed.is_set(),
                                             f"Setup timeout cannot report transport success; {phase_diagnostic()}")
                        expected = "COMPLETED" if mode == "positive" else "CANCELLED" if mode == "cancel" else "DEAD_LETTERED"
                        reopened = Store(url)
                        try:
                            with TestClient(create_app(reopened, "http://127.0.0.1:1", root,
                                    "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as fresh:
                                saved = fresh.get(f"/operations/{identifier}").json()
                                phases["terminal_readback_at"] = time.monotonic()
                                phases["terminal_state"] = saved.get("state")
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
                        if unprepared_ssl:
                            self.assertEqual(state["connects"], 0)
                            self.assertIsNone(state.get("connecting_socket"))
                        elif mode != "positive":
                            self.assertEqual(state["connects"], 1)
                            self.assertEqual(state["connecting_socket"].fileno(), -1)
                        if not unprepared_ssl:
                            if provider in ("greenhouse", "remoteok"):
                                self.assertTrue(ssl_context_uses,
                                    f"Owned {provider} transport did not use its ready SSL context")
                                if provider == "greenhouse":
                                    self.assertTrue(any(thread is not worker for thread in ssl_context_threads),
                                        "Owned greenhouse request pool did not use its ready SSL context")
                            self.assertTrue(all(used_at >= ssl_context_ready_at for used_at in ssl_context_uses))
                        print(f"OWNED_ADAPTER_CONNECT provider={provider} tenant={tenant} mode={mode} latency={elapsed:.3f}s")
                        if mode == "cancel":
                            print(f"OWNED_ADAPTER_CANCEL_BOUNDARY response={cancel_response_elapsed:.3f}s "
                                  f"dispatch={cancel_dispatch_elapsed:.3f}s "
                                  f"service_response={cancel_service_response_elapsed:.3f}s "
                                  f"store={cancel_store_elapsed:.3f}s "
                                  f"response_tail={cancel_response_tail_elapsed:.3f}s "
                                  f"transport={transport_settle_elapsed:.3f}s "
                                  f"worker={worker_settle_elapsed:.3f}s "
                                  f"worker_after_response={worker_after_response_elapsed:.3f}s")
                            self.assertLess(cancel_service_response_elapsed, .35,
                                f"{provider}: cancel service response blocked {cancel_service_response_elapsed:.3f}s; "
                                f"total_response={cancel_response_elapsed:.3f}s; "
                                f"dispatch={cancel_dispatch_elapsed:.3f}s; "
                                f"store={cancel_store_elapsed:.3f}s; "
                                f"response_tail={cancel_response_tail_elapsed:.3f}s")
                            self.assertLess(transport_settle_elapsed, .35,
                                f"{provider}: pending transport did not stop within 0.35s; "
                                f"cancel_response={cancel_response_elapsed:.3f}s; "
                                f"transport_settle={transport_settle_elapsed:.3f}s; "
                                f"worker_settle={worker_settle_elapsed:.3f}s; "
                                f"worker_after_response={worker_after_response_elapsed:.3f}s; "
                                f"pending={pending and (pending['state'], pending['cancel_requested'])}; "
                                f"durable={saved['state']}/{saved['error']}; HTTP={state['requests']}")
                    finally:
                        collection_release.set()
                        deadline_guard_release.set()
                        if worker.is_alive():
                            client.post(f"/operations/{identifier}/cancel")
                        worker.join(timeout=3)
                        self.assertFalse(worker.is_alive(), "Owned adapter worker leaked")

    def test_greenhouse_public_cancel_during_real_tcp_connect(self):
        self.run_source("greenhouse", "cancel")

    def test_greenhouse_ssl_readiness_rejects_outer_worker_only_scope(self):
        # Recreate the old ownership boundary. Healthy real HTTP requests still
        # complete, but the fixture must reject bypassed prepared SSL readiness.
        with patch.object(OwnedRequestThreads, "owns_current_thread",
                          new=lambda scope: threading.current_thread() is scope.worker):
            with self.assertRaisesRegex(AssertionError,
                                       "Owned greenhouse transport did not use its ready SSL context"):
                self.run_source("greenhouse", "positive")

    def test_remoteok_public_cancel_during_real_tcp_connect(self):
        self.run_source("remoteok", "cancel", worker_unwind_delay=.4)

    def test_wwr_public_cancel_during_real_tcp_connect(self):
        self.run_source("wwr", "cancel")

    def test_public_cancel_budget_excludes_fixture_dispatch_delay(self):
        self.run_source("greenhouse", "cancel", cancel_dispatch_delay=.8)

    def test_public_cancel_budget_rejects_delay_after_handler_admission(self):
        with self.assertRaisesRegex(AssertionError, "cancel service response blocked"):
            self.run_source("greenhouse", "cancel", cancel_service_delay=.4)

    def test_positive_public_worker_collections_and_fresh_readback(self):
        for provider in ENDPOINTS:
            with self.subTest(provider=provider):
                self.run_source(provider, "positive")

    def test_finite_worker_deadline_during_real_tcp_connect(self):
        for provider in ENDPOINTS:
            with self.subTest(provider=provider):
                self.run_source(provider, "deadline",
                                coordinator_delay=1.05 if provider == "remoteok" else 0,
                                ssl_setup_delay=1.1 if provider in ("greenhouse", "remoteok") else 0)

    def test_greenhouse_cold_ssl_setup_exhausts_worker_deadline_before_connect(self):
        self.run_source("greenhouse", "cold_ssl_deadline", ssl_setup_delay=1.1)

    def test_greenhouse_real_deadline_expires_before_ssl_setup_without_connect(self):
        self.run_source("greenhouse", "pre_ssl_deadline",
                        transport_setup_delay=1.0, ssl_setup_delay=1.1)

    def test_remoteok_cold_ssl_setup_exhausts_worker_deadline_before_connect(self):
        # Genuine SSL setup must consume the original logical deadline even
        # when unrelated fixture scheduling would exceed its real-time budget.
        self.run_source("remoteok", "cold_ssl_deadline", ssl_setup_delay=1.1)

    def test_remoteok_real_deadline_expires_before_ssl_setup_without_connect(self):
        self.run_source("remoteok", "pre_ssl_deadline",
                        transport_setup_delay=1.0, ssl_setup_delay=1.1)

    def test_remoteok_cold_ssl_accounting_survives_pre_ssl_fixture_delay(self):
        self.run_source("remoteok", "cold_ssl_deadline",
                        transport_setup_delay=1.0, ssl_setup_delay=1.1)

    def test_lever_global_public_cancel_during_real_tcp_connect(self):
        self.run_source("lever", "cancel", tenant="global:synthetic",
                        transport_setup_delay=1.1)

    def test_lever_eu_public_cancel_during_real_tcp_connect(self):
        self.run_source("lever", "cancel", tenant="eu:synthetic")

    def test_lever_eu_deadline_survives_delayed_coordinator_before_worker_entry(self):
        self.run_source("lever", "deadline", tenant="eu:synthetic", coordinator_delay=1.05)

    def test_lever_global_finite_deadline_during_real_tcp_connect(self):
        self.run_source("lever", "deadline", tenant="global:synthetic")

    def test_lever_eu_finite_deadline_during_real_tcp_connect(self):
        self.run_source("lever", "deadline", tenant="eu:synthetic", coordinator_delay=1.05)

    def test_lever_global_healthy_worker_and_fresh_source_readback(self):
        self.run_source("lever", "positive", tenant="global:synthetic")

    def test_lever_eu_healthy_worker_and_fresh_source_readback(self):
        self.run_source("lever", "positive", tenant="eu:synthetic")
