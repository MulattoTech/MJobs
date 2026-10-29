"""MJ-REQ-004: real TCP source reads below HTTPX's 64KiB aggregate.

Public admission and execute_operation create the production cancellation/deadline
check. Only its timeout setting and the provider endpoint are fixture seams.
HTTPX transport, response streams, sockets, worker check and watchdog are real.
"""
from contextlib import contextmanager
import os
import ipaddress
from pathlib import Path
import socket
import select as socket_select
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers import tasks as worker_tasks
from workers.tasks import execute_operation
from services.discovery.sources import http_transport
from services.discovery.sources.remotive import Remotive, SourceError
from tests.unit.test_ai_tls_handshake import tls_contexts


@contextmanager
def tcp_connect_stall():
    """Fill only this listener's accept backlog; never accept a connection."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    address = listener.getsockname()
    owned = []
    attempted = threading.Event()
    state = {"bytes": 0, "requests": 0, "connection": None, "address": address, "connects": 0}
    try:
        for _ in range(16):
            filler = socket.socket()
            filler.settimeout(.05)
            try:
                filler.connect(address)
            except socket.timeout:
                filler.close()
                break
            owned.append(filler)
        else:
            raise AssertionError("Owned loopback listener backlog did not saturate within16 sockets")
        yield f"http://127.0.0.1:{address[1]}/source", attempted, state
    finally:
        listener.close()
        for connection in owned:
            connection.close()


@contextmanager
def tcp_source(mode, *, tls_context=None):
    if mode == "positive-hostname":
        # Bind the hosts-file lookup's preferred loopback family. A listener
        # restricted to IPv4 cannot serve the preferred ::1 address on Windows.
        family, kind, proto, _name, target = socket.getaddrinfo("localhost", 0, type=socket.SOCK_STREAM)[0]
        self_address = target
        if not ipaddress.ip_address(target[0]).is_loopback:
            raise AssertionError("Refusing non-loopback localhost fixture")
    else:
        family, kind, proto, self_address = socket.AF_INET, socket.SOCK_STREAM, 0, ("127.0.0.1", 0)
    listener = socket.socket(family, kind, proto)
    listener.bind(self_address)
    listener.listen(1)
    listener.settimeout(.1)
    stop, engaged = threading.Event(), threading.Event()
    state = {"bytes": 0, "requests": 0, "connection": None, "address": listener.getsockname()[:2],
             "peer_closed": threading.Event()}

    def serve():
        connection = None
        try:
            deadline = time.monotonic() + 3
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    connection, _ = listener.accept()
                    break
                except socket.timeout:
                    continue
            if connection is None:
                return
            state["connection"] = connection
            if tls_context is not None:
                connection.settimeout(2)
                connection = tls_context.wrap_socket(connection, server_side=True)
                state["connection"] = connection
            connection.settimeout(.1)
            if mode == "tls-stall":
                # Observe a real TLS ClientHello before withholding handshake
                # bytes. There is no HTTP request or certificate exception seam.
                hello = connection.recv(4096)
                if hello[:1] != b"\x16":
                    raise AssertionError("Expected real TLS handshake record")
                state["bytes"] = len(hello)
                state["engaged_at"] = time.monotonic()
                engaged.set()
                while not stop.is_set() and time.monotonic() < deadline:
                    try:
                        if not connection.recv(4096):
                            state["peer_closed"].set()
                            break
                    except socket.timeout:
                        continue
                    except (ConnectionResetError, ConnectionAbortedError):
                        state["peer_closed"].set()
                        break
                return
            request = bytearray()
            while b"\r\n\r\n" not in request:
                if stop.is_set() or time.monotonic() > deadline:
                    return
                try:
                    part = connection.recv(1024)
                except socket.timeout:
                    continue
                if not part:
                    return
                request.extend(part)
            state["requests"] += 1
            if mode in {"positive", "positive-hostname"}:
                body = b'{"jobs": []}'
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                   + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                state["bytes"] = len(body)
                engaged.set()
                return
            if mode == "body-drip":
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                   b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
            elif mode == "header-drip":
                connection.sendall(b"HTTP/1.1 200 OK\r\n")
            engaged.set()
            while not stop.wait(.02) and time.monotonic() < deadline:
                if mode == "body-drip":
                    payload = b" " * 8
                    connection.sendall(b"8\r\n" + payload + b"\r\n")
                    state["bytes"] += len(payload)
                elif mode == "header-drip":
                    connection.sendall(b"X-Fixture: 1\r\n")
                    state["bytes"] += 14
                # header-stall accepts TCP and reads the request, then sends
                # no response bytes. This is not a TCP-connect-stall claim.
        except OSError:
            # A body reader that honors cancellation closes its owned response
            # socket; expose that fact to the deadline integration oracle.
            if mode == "body-drip":
                state["peer_closed"].set()
            pass  # The real client watchdog can close its owned connection.
        finally:
            if connection is not None:
                connection.close()

    thread = threading.Thread(target=serve, name="mjobs-owned-source-tcp", daemon=True)
    thread.start()
    try:
        scheme = "https" if mode == "tls-stall" or tls_context is not None else "http"
        host = "localhost" if mode == "positive-hostname" else "127.0.0.1"
        yield f"{scheme}://{host}:{listener.getsockname()[1]}/source", engaged, state
    finally:
        stop.set()
        connection = state["connection"]
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        listener.close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError("Owned loopback source server did not stop")


class SourceHttpSocketBudgetTests(unittest.TestCase):
    def run_source(self, mode, *, cancel=False, worker_budget=.8, setup_delay=0,
                   unwind_delay=0, worker_entry_delay=0):
        with tempfile.TemporaryDirectory(prefix="mjobs-source-tcp-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'source.db').as_posix()}"
            store = Store(database_url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            outcome, errors = [], []
            worker = None
            try:
                source = tcp_connect_stall() if mode == "connect-stall" else tcp_source(mode)
                original_connect = socket.socket.connect
                original_connect_ex = socket.socket.connect_ex
                original_collect_source = worker_tasks.collect_source
                original_connect_tcp = http_transport._Backend.connect_tcp
                original_start_tls = http_transport._Stream.start_tls
                transport_entered = threading.Event()
                connect_exited = threading.Event()
                connect_exit = {}
                tls_entered = threading.Event()
                tls_entry = {}
                tls_exited = threading.Event()
                tls_exit = {}

                def delayed_collect_source(*args, **kwargs):
                    try:
                        if setup_delay:
                            time.sleep(setup_delay)
                        return original_collect_source(*args, **kwargs)
                    finally:
                        # Model a loaded shard delaying worker bookkeeping only
                        # after the transport has already honored its deadline,
                        # then re-enter the real worker fence before preserving
                        # an adapter error from that earlier transport phase.
                        if unwind_delay:
                            time.sleep(unwind_delay)
                            kwargs["check"]()

                def observe_connect_tcp(backend, *args, **kwargs):
                    transport_entered.set()
                    try:
                        return original_connect_tcp(backend, *args, **kwargs)
                    finally:
                        connecting = socket_state.get("connecting_socket")
                        connect_exit["at"] = time.monotonic()
                        connect_exit["fileno"] = (connecting.fileno()
                                                  if connecting is not None else None)
                        connect_exited.set()

                def observe_start_tls(stream, *args, **kwargs):
                    tls_entry["at"] = time.monotonic()
                    tls_entered.set()
                    try:
                        return original_start_tls(stream, *args, **kwargs)
                    finally:
                        tls_exit["at"] = time.monotonic()
                        tls_exit["fileno"] = stream.connection.fileno()
                        tls_exited.set()

                def observe_connect(connection, address):
                    if mode == "connect-stall" and address[:2] == socket_state["address"]:
                        socket_state["connects"] += 1
                        socket_state["connecting_socket"] = connection
                        engaged.set()
                    return original_connect(connection, address)

                def observe_connect_ex(connection, address):
                    status = original_connect_ex(connection, address)
                    if address[:2] == socket_state["address"]:
                        socket_state["connecting_socket"] = connection
                    if mode == "connect-stall" and address[:2] == socket_state["address"]:
                        socket_state["connects"] += 1
                        socket_state["connecting_socket"] = connection
                        engaged.set()
                    return status

                with source as (endpoint, engaged, socket_state), patch.dict(os.environ, {
                    "MJOBS_DATABASE_URL": database_url, "MJOBS_OPERATION_MODE": "FIXTURE",
                    "MJOBS_OUTPUT_DIR": directory, "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
                }), patch("socket.socket.connect", new=observe_connect), patch(
                        "socket.socket.connect_ex", new=observe_connect_ex), patch(
                        "workers.tasks.collect_source", new=delayed_collect_source), patch(
                        "services.discovery.sources.http_transport._Backend.connect_tcp",
                        new=observe_connect_tcp), patch(
                        "services.discovery.sources.http_transport._Stream.start_tls",
                        new=observe_start_tls), patch(
                        "services.discovery.sources.remotive.API_URL", endpoint), patch(
                        "workers.tasks.timeout", return_value=worker_budget):
                    with TestClient(create_app(store, "http://127.0.0.1:1", root,
                                               "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
                        self.assertEqual(client.put("/profiles", json=PROFILE).status_code, 200)
                        configured = client.put("/sources", json={
                            "provider": "remotive", "tenant": "default", "request_timeout": 2,
                        })
                        self.assertEqual(configured.status_code, 200, configured.text)
                        source_id = configured.json()["id"]
                        admitted = client.post(f"/sources/{source_id}/collect", json={
                            "profile_id": PROFILE["id"], "request_id": uuid4().hex,
                        })
                        self.assertEqual(admitted.status_code, 202, admitted.text)
                        identifier = admitted.json()["operation_id"]

                        def execute():
                            try:
                                if worker_entry_delay:
                                    time.sleep(worker_entry_delay)
                                outcome.append(execute_operation(identifier))
                            except BaseException as error:
                                errors.append(error)

                        started = time.monotonic()
                        worker = threading.Thread(target=execute, name="mjobs-owned-source-worker", daemon=True)
                        worker.start()
                        try:
                            transport_elapsed = None
                            if mode == "connect-stall":
                                self.assertTrue(transport_entered.wait(3),
                                                "Worker did not reach source HTTP transport")
                            if mode == "tls-stall" and not cancel:
                                self.assertTrue(tls_entered.wait(3),
                                                "Worker did not reach TLS transport after fixture setup")
                                tls_deadline_end = tls_entry["at"] + 1.1
                                engagement_wait = max(0, tls_deadline_end - time.monotonic())
                                self.assertTrue(engaged.wait(engagement_wait),
                                                f"Real loopback TLS operation did not engage within "
                                                f"the transport deadline; entry={tls_entry}")
                                engagement_elapsed = socket_state["engaged_at"] - tls_entry["at"]
                                self.assertLess(engagement_elapsed, 1.1,
                                                f"TLS ClientHello engagement exceeded the transport "
                                                f"deadline: {engagement_elapsed:.3f}s")
                            else:
                                self.assertTrue(engaged.wait(3 if mode == "positive-hostname" else 1),
                                                "Real loopback TCP operation did not engage")
                            if cancel:
                                if mode == "connect-stall":
                                    connecting = socket_state["connecting_socket"]
                                    self.assertGreaterEqual(connecting.fileno(), 0)
                                    self.assertFalse(socket_select.select([], [connecting], [], 0)[1],
                                                     "Connect must still be pending at public cancellation")
                                requested = time.monotonic()
                                response = client.post(f"/operations/{identifier}/cancel")
                                self.assertEqual(response.status_code, 202, response.text)
                                worker.join(timeout=.35)
                                if worker.is_alive():
                                    pending = client.get(f"/operations/{identifier}").json()
                                    # Observe natural completion before closing
                                    # the fixture listener, to distinguish a
                                    # cancelled-but-blocked connect from failed
                                    # admission or forced test cleanup.
                                    worker.join(timeout=2.2)
                                    completed = client.get(f"/operations/{identifier}").json()
                                    self.fail(f"Cancellation stalled {time.monotonic()-requested:.3f}s; "
                                              f"pending={pending['state']}/cancel={pending['cancel_requested']}; "
                                              f"final={completed['state']}/{completed['error']}; "
                                              f"HTTP_requests={socket_state['requests']}")
                                self.assertFalse(worker.is_alive(),
                                                 f"Cancellation ignored during {mode} for {time.monotonic()-requested:.3f}s")
                            else:
                                if mode == "connect-stall":
                                    self.assertTrue(connect_exited.wait(1.1),
                                                    "TCP connect transport ignored the operation deadline")
                                    transport_elapsed = connect_exit["at"] - started
                                    self.assertLess(transport_elapsed, 1.1,
                                                    f"TCP connect transport exceeded the operation deadline: "
                                                    f"{transport_elapsed:.3f}s; exit={connect_exit}")
                                    self.assertEqual(connect_exit["fileno"], -1,
                                                     f"Timed-out TCP connect retained its socket: "
                                                     f"exit={connect_exit}")
                                    # Terminal persistence is separately bounded:
                                    # loaded shards may schedule it after the
                                    # transport has already closed on deadline.
                                    worker.join(timeout=3)
                                elif mode == "tls-stall":
                                    self.assertTrue(tls_exited.wait(1.1),
                                                    "TLS transport ignored the operation deadline")
                                    transport_elapsed = tls_exit["at"] - tls_entry["at"]
                                    self.assertLess(transport_elapsed, 1.1,
                                                    "TLS transport exceeded the operation deadline")
                                    self.assertEqual(tls_exit["fileno"], -1,
                                                     "Timed-out TLS transport retained its socket")
                                    # Terminal persistence is separately bounded:
                                    # loaded shards may schedule it after the
                                    # transport has already closed on deadline.
                                    worker.join(timeout=3)
                                else:
                                    worker.join(timeout=3 if mode == "positive-hostname" else 1.1)
                                self.assertFalse(worker.is_alive(),
                                                 f"Operation deadline ignored during {mode} for {time.monotonic()-started:.3f}s")
                            settled_elapsed = time.monotonic() - (requested if cancel else started)
                            self.assertEqual(errors, [])
                            self.assertEqual(len(outcome), 1)
                            saved = client.get(f"/operations/{identifier}").json()
                            expected = "COMPLETED" if mode.startswith("positive") else "CANCELLED" if cancel else "DEAD_LETTERED"
                            self.assertEqual(saved["state"], expected, saved)
                            if expected != "COMPLETED":
                                self.assertEqual(saved["error"], "CANCEL_REQUESTED" if cancel else "OPERATION_TIMEOUT")
                            self.assertEqual(saved["attempt_count"], 1)
                            reopened = Store(database_url)
                            try:
                                with TestClient(create_app(reopened, "http://127.0.0.1:1", root,
                                        "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as fresh:
                                    durable = fresh.get(f"/operations/{identifier}").json()
                                    self.assertEqual((durable["state"], durable["error"], durable["attempt_count"]),
                                                     (saved["state"], saved["error"], 1))
                            finally:
                                reopened.engine.dispose()
                            self.assertEqual(socket_state["requests"], 0 if mode in {"connect-stall", "tls-stall"} else 1)
                            if mode == "connect-stall":
                                self.assertEqual(socket_state["connects"], 1)
                            if "connecting_socket" in socket_state:
                                self.assertEqual(socket_state["connecting_socket"].fileno(), -1)
                            if mode == "tls-stall":
                                self.assertTrue(socket_state["peer_closed"].wait(.3),
                                                "Owned TLS connection remained alive after interruption")
                            if mode == "body-drip":
                                self.assertTrue(socket_state["peer_closed"].wait(.3),
                                                "Remotive body response remained open after worker deadline")
                            print(f"OWNED_SOURCE_BUDGET mode={mode} cancel={cancel} "
                                  f"latency={settled_elapsed:.3f}s"
                                  + (f" transport={transport_elapsed:.3f}s"
                                     if transport_elapsed is not None else ""))
                            self.assertLess(socket_state["bytes"], 65536)
                            with store.engine.connect() as connection:
                                source = connection.execute(select(m.job_sources).where(
                                    m.job_sources.c.id == source_id)).mappings().one()
                            self.assertEqual(source["health_status"], "HEALTHY" if expected == "COMPLETED" else "NOT_CHECKED")
                            self.assertEqual(source["consecutive_failures"], 0)
                            self.assertIsNone(source["last_error"])
                            self.assertEqual(store.count("canonical_jobs"), 0)
                            return saved
                        finally:
                            # Close only this fixture's accepted socket to bound
                            # even a failed cancellation/deadline oracle.
                            connection = socket_state["connection"]
                            if connection is not None:
                                try:
                                    connection.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                            worker.join(timeout=3)
                            self.assertFalse(worker.is_alive(), "Owned worker leaked after fixture socket shutdown")
            finally:
                store.engine.dispose()

    def test_positive_public_worker_source_uses_real_socket(self):
        self.run_source("positive")

    def test_positive_public_worker_source_uses_owned_localhost_dns(self):
        # DNS uses the real owned subprocess; keep production source timeout.
        self.run_source("positive-hostname", worker_budget=30)

    def test_worker_deadline_under_continuous_sub_64k_body_and_header_stall(self):
        for mode in ("body-drip", "header-drip", "header-stall"):
            with self.subTest(mode=mode):
                self.run_source(mode)

    def test_public_cancel_under_continuous_sub_64k_body_and_header_stall(self):
        for mode in ("body-drip", "header-drip", "header-stall"):
            with self.subTest(mode=mode):
                self.run_source(mode, cancel=True)

    def test_public_cancel_during_real_tcp_connect_stall(self):
        # Valid production operation budget; the source's minimum supported
        # two-second connect timeout must not become the cancellation latency.
        self.run_source("connect-stall", cancel=True, worker_budget=30)

    def test_connect_stall_oracle_starts_after_worker_setup(self):
        self.run_source("connect-stall", cancel=True, worker_budget=30, setup_delay=1.1)

    def test_worker_deadline_during_real_tcp_connect_stall(self):
        self.run_source("connect-stall", unwind_delay=.8)

    def test_public_cancel_during_real_tls_handshake_stall(self):
        self.run_source("tls-stall", cancel=True, worker_budget=30)

    def test_worker_deadline_during_real_tls_handshake_stall(self):
        self.run_source("tls-stall", unwind_delay=.8, worker_entry_delay=1.1)

    def test_owned_dns_child_is_reaped_on_cancel_or_deadline_without_resolver_threads(self):
        class Cancelled(Exception):
            pass

        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                children = []
                original = subprocess.Popen

                def spawn(*args, **kwargs):
                    child = original(*args, **kwargs)
                    self.assertIsNone(child.poll())
                    children.append(child)
                    return child

                def check():
                    if cancel and children:
                        raise Cancelled()

                before = {thread.ident for thread in threading.enumerate()}
                with patch.object(http_transport, "_RESOLVER_CODE", "import time; time.sleep(10)"), patch.object(
                        http_transport.subprocess, "Popen", side_effect=spawn):
                    expected = Cancelled if cancel else http_transport.httpcore.ConnectTimeout
                    started = time.monotonic()
                    with self.assertRaises(expected):
                        http_transport._resolve("localhost", 80, check, started + .15)
                    self.assertLess(time.monotonic() - started, .5)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(children[0].stdout.closed)
                self.assertEqual({thread.ident for thread in threading.enumerate()}, before)
                # A subsequent real hosts-file lookup proves the slot and
                # subprocess output resources were released after interruption.
                addresses = http_transport._resolve("localhost", 80, lambda: None, time.monotonic() + 2)
                self.assertTrue(addresses)

    def test_dns_capacity_wait_is_bounded_without_spawning_an_extra_resolver(self):
        with patch.object(http_transport, "_DNS_SLOTS", threading.BoundedSemaphore(0)), patch.object(
                http_transport.subprocess, "Popen") as spawn:
            started = time.monotonic()
            with self.assertRaises(http_transport.httpcore.ConnectTimeout):
                http_transport._resolve("localhost", 80, lambda: None, started + .08)
            self.assertLess(time.monotonic() - started, .2)
            spawn.assert_not_called()

    def test_dns_launch_failure_is_classified_and_releases_capacity(self):
        with patch.object(http_transport.subprocess, "Popen", side_effect=OSError("synthetic process denial")):
            with self.assertRaises(http_transport.httpcore.ConnectError):
                http_transport._resolve("localhost", 80, lambda: None, time.monotonic() + 1)
        self.assertTrue(http_transport._resolve("localhost", 80, lambda: None, time.monotonic() + 2))

    def test_default_transport_preserves_source_trust_env_false(self):
        with patch.dict(os.environ, {"SSL_CERT_FILE": "synthetic-nonexistent-certificate.pem"}):
            transport = http_transport.SourceTransport(lambda: None)
            transport.close()

    def test_pending_first_address_preserves_other_address_opportunity_and_closes_losers(self):
        with tcp_connect_stall() as (_blocked, _engaged, blocked), tcp_source("positive") as (endpoint, _ready, healthy):
            addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", state["address"])
                         for state in (blocked, healthy)]
            created = []
            original = socket.socket
            owner_thread = threading.get_ident()

            def record(*args, **kwargs):
                connection = original(*args, **kwargs)
                if threading.get_ident() == owner_thread:
                    created.append(connection)
                return connection

            with patch.object(http_transport, "_resolve", return_value=addresses), patch.object(
                    http_transport.socket, "socket", side_effect=record):
                with httpx.Client(transport=http_transport.SourceTransport(lambda: None), timeout=.5,
                                  trust_env=False) as client:
                    self.assertEqual(client.get(endpoint).json(), {"jobs": []})
            self.assertEqual(len(created), 2)
            self.assertTrue(all(connection.fileno() == -1 for connection in created))

    def test_real_tls_preserves_certificate_and_hostname_verification(self):
        with tls_contexts() as (server_context, client_context):
            for scenario in ("trusted", "untrusted", "hostname_mismatch"):
                mode = "positive-hostname" if scenario == "hostname_mismatch" else "positive"
                with self.subTest(scenario=scenario), tcp_source(mode, tls_context=server_context) as (
                        endpoint, _engaged, state), patch("services.discovery.sources.remotive.API_URL", endpoint):
                    transport = http_transport.SourceTransport(
                        lambda: None, verify=True if scenario == "untrusted" else client_context)
                    adapter = Remotive(transport=transport, request_timeout=2)
                    if scenario == "trusted":
                        self.assertEqual(adapter.search().items, ())
                        self.assertEqual(state["requests"], 1)
                        self.assertEqual(adapter.health(), "HEALTHY")
                    else:
                        with self.assertRaisesRegex(SourceError, "SOURCE_NETWORK_ERROR"):
                            adapter.search()
                        self.assertEqual(state["requests"], 0)
                    self.assertTrue(transport._pool._connections == [])
