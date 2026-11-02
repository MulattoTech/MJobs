"""Deadline clock remains independent of real database lock contention."""
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m
from packages.persistence.store import Store
from services.ai.providers import AIProviderError
from services.ai.providers.transport import _RequestGuard, request_guard
from tests.unit.test_operation_control_reads import seed_operation
from workers.operations import OperationControlUnavailable, check_cancelled


class IndependentRequestClockTests(unittest.TestCase):
    def test_deadline_closes_owned_socket_while_real_database_observer_is_blocked(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-watchdog-db-") as root, ExitStack() as resources:
            database = Path(root) / "observer.db"
            with closing(sqlite3.connect(database)) as setup:
                setup.execute("CREATE TABLE authority (cancelled INTEGER)")
                setup.execute("INSERT INTO authority VALUES (0)")
                setup.commit()
            blocker = resources.enter_context(closing(sqlite3.connect(database)))
            left, peer = socket.socketpair()
            peer.settimeout(2)
            resources.enter_context(closing(left))
            resources.enter_context(closing(peer))
            armed, entered, returned = threading.Event(), threading.Event(), threading.Event()

            class Cancellation:
                def is_set(self):
                    if not armed.is_set():
                        return False
                    entered.set()
                    # A genuine DB lock, with a longer budget than the request,
                    # demonstrates that the clock never calls this observer.
                    with closing(sqlite3.connect(database, timeout=2)) as connection:
                        result = bool(connection.execute("SELECT cancelled FROM authority").fetchone()[0])
                    returned.set()
                    return result

            before = {t.ident for t in threading.enumerate()}
            with (httpx.Client() as client, self.assertRaises(AIProviderError) as failed,
                  request_guard(client, deadline=time.monotonic() + .4,
                                cancellation_event=Cancellation()) as guard):
                guard.register_connecting(left)
                blocker.execute("BEGIN EXCLUSIVE")
                armed.set()
                try:
                    self.assertTrue(entered.wait(1), "Observer never entered its real SQL read")
                    self.assertFalse(returned.is_set())
                    started = time.monotonic()
                    self.assertEqual(peer.recv(1), b"")
                    self.assertLess(time.monotonic() - started, .8)
                    self.assertFalse(returned.is_set(), "Deadline must interrupt before the DB lock releases")
                    self.assertEqual(guard.interruption_code, "AI_PROVIDER_TIMEOUT")
                finally:
                    blocker.rollback()
            self.assertEqual(failed.exception.code, "AI_PROVIDER_TIMEOUT")
            self.assertTrue(returned.is_set())
            self.assertFalse([t for t in threading.enumerate() if t.ident not in before
                              and t.name in {"mjobs-request-clock", "mjobs-request-deadline"}])
            self.assertEqual(left.fileno(), -1)

    def test_callback_is_never_invoked_under_socket_ownership_lock(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        class Cancellation:
            def is_set(self):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("fixture did not release observer")
                return False
        left, peer = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(left.close)
        with httpx.Client() as client:
            guard = _RequestGuard(client, time.monotonic() + 10, Cancellation())
            def register():
                try:
                    guard.register_connecting(left)
                except AIProviderError as error:
                    errors.append(error.code)
            thread = threading.Thread(target=register, name="owned-register")
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                guard.interrupt("AI_PROVIDER_TIMEOUT", close_resources=False)
                self.assertLess(time.monotonic() - started, .3)
                self.assertEqual(left.fileno(), -1, "The clock must already own the socket during the observer")
            finally:
                release.set()
                thread.join(timeout=3)
                guard.close()
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["AI_PROVIDER_TIMEOUT"])
            self.assertEqual(left.fileno(), -1)

    def test_observer_exception_is_fixed_unavailable_and_closes_owned_socket(self):
        class BrokenObserver:
            def is_set(self):
                raise RuntimeError("synthetic-private-db-url-and-prompt")
        with httpx.Client() as client:
            guard = _RequestGuard(client, time.monotonic() + 10, None)
            left, peer = socket.socketpair()
            self.addCleanup(left.close)
            self.addCleanup(peer.close)
            guard.register_connecting(left)
            guard.cancellation_event = BrokenObserver()
            with self.assertRaises(AIProviderError) as error:
                guard.check()
            self.assertEqual(error.exception.code, "AI_PROVIDER_UNAVAILABLE")
            self.assertNotIn("synthetic-private", str(error.exception))
            self.assertEqual(left.fileno(), -1)
            guard.close()


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "Disposable PostgreSQL URL required")
class PostgresOperationControlLockTests(unittest.TestCase):
    def test_real_table_lock_has_bounded_read_and_transaction_local_settings(self):
        url = os.environ["MJOBS_TEST_POSTGRES_URL"]
        schema = "mjobs_control_" + uuid4().hex
        admin = create_engine(url)
        store = Store(url)
        store.engine = store.engine.execution_options(schema_translate_map={None: schema})
        try:
            with admin.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            m.operations.create(store.engine)
            seed_operation(store)
            with store.engine.connect() as connection:
                original = tuple(connection.execute(text(
                    "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")).one())
            with admin.begin() as blocker:
                blocker.execute(text(f'LOCK TABLE "{schema}".worker_operations IN ACCESS EXCLUSIVE MODE'))
                started = time.monotonic()
                with self.assertRaises(OperationControlUnavailable) as failure:
                    check_cancelled(store, "owned-control", 1)
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, .06)
                self.assertLess(elapsed, 1)
                self.assertEqual(str(failure.exception), "OPERATION_CONTROL_UNAVAILABLE")
                self.assertEqual(store.engine.pool.checkedout(), 0)
            check_cancelled(store, "owned-control", 1)
            with store.engine.connect() as connection:
                restored = tuple(connection.execute(text(
                    "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")).one())
                self.assertEqual(restored, original)
                row = connection.execute(select(m.operations)).mappings().one()
                self.assertEqual((row["state"], row["cancel_requested"]), ("RUNNING", False))
        finally:
            store.engine.dispose()
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            admin.dispose()
