"""MJ-REQ-004: real TCP source reads below HTTPX's 64KiB aggregate.

Public admission and execute_operation create the production cancellation/deadline
check. Only its timeout setting and the provider endpoint are fixture seams.
HTTPX transport, response streams, sockets, worker check and watchdog are real.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


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
def tcp_source(mode):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(.1)
    stop, engaged = threading.Event(), threading.Event()
    state = {"bytes": 0, "requests": 0, "connection": None}

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
            connection.settimeout(.1)
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
            if mode == "positive":
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
            pass  # The real client watchdog can close its owned connection.
        finally:
            if connection is not None:
                connection.close()

    thread = threading.Thread(target=serve, name="mjobs-owned-source-tcp", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/source", engaged, state
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
    def run_source(self, mode, *, cancel=False, worker_budget=.8):
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

                def observe_connect(connection, address):
                    if mode == "connect-stall" and address[:2] == socket_state["address"]:
                        socket_state["connects"] += 1
                        engaged.set()
                    return original_connect(connection, address)

                with source as (endpoint, engaged, socket_state), patch.dict(os.environ, {
                    "MJOBS_DATABASE_URL": database_url, "MJOBS_OPERATION_MODE": "FIXTURE",
                    "MJOBS_OUTPUT_DIR": directory, "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
                }), patch("socket.socket.connect", new=observe_connect), patch(
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
                                outcome.append(execute_operation(identifier))
                            except BaseException as error:
                                errors.append(error)

                        started = time.monotonic()
                        worker = threading.Thread(target=execute, name="mjobs-owned-source-worker", daemon=True)
                        worker.start()
                        try:
                            self.assertTrue(engaged.wait(1), "Worker did not reach real loopback TCP server")
                            if cancel:
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
                                worker.join(timeout=1.1)
                                self.assertFalse(worker.is_alive(),
                                                 f"Operation deadline ignored during {mode} for {time.monotonic()-started:.3f}s")
                            self.assertEqual(errors, [])
                            self.assertEqual(len(outcome), 1)
                            saved = client.get(f"/operations/{identifier}").json()
                            expected = "COMPLETED" if mode == "positive" else "CANCELLED" if cancel else "DEAD_LETTERED"
                            self.assertEqual(saved["state"], expected, saved)
                            if expected != "COMPLETED":
                                self.assertEqual(saved["error"], "CANCEL_REQUESTED" if cancel else "OPERATION_TIMEOUT")
                            self.assertEqual(saved["attempt_count"], 1)
                            self.assertEqual(socket_state["requests"], 0 if mode == "connect-stall" else 1)
                            if mode == "connect-stall":
                                self.assertEqual(socket_state["connects"], 1)
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

    def test_worker_deadline_during_real_tcp_connect_stall(self):
        self.run_source("connect-stall")
