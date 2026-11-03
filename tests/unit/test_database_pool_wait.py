"""Real pool saturation cannot hold worker authority checks for 30 seconds."""
from contextlib import ExitStack
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.pool import SingletonThreadPool

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.unit.test_operation_control_reads import seed_operation
from workers.operations import OperationControlUnavailable, check_cancelled


class PoolWaitContract:
    """Exercise the production Store pool; no pool internals or fake checkout."""

    def hold_capacity(self, resources, store=None):
        # Store retains SQLAlchemy's five persistent plus ten overflow slots.
        store = store or self.store
        held = [resources.enter_context(store.engine.connect()) for _ in range(15)]
        self.assertEqual(store.engine.pool.checkedout(), 15)
        self.assertEqual(store.engine.pool.overflow(), 10)
        return held

    def start_waiter(self, action):
        entered, done = threading.Event(), threading.Event()
        results = []

        def run():
            entered.set()
            try:
                action()
                results.append(None)
            except Exception as error:
                results.append(error)
            finally:
                done.set()

        waiter = threading.Thread(target=run, name="owned-pool-wait-fixture")
        waiter.start()
        self.addCleanup(waiter.join, 3)
        self.assertTrue(entered.wait(2), "Owned waiter did not start")
        return waiter, done, results

    def assert_saturated_control_is_safe(self):
        with ExitStack() as resources:
            held = self.hold_capacity(resources)
            caller = held[0]
            transaction = caller.begin()
            caller.execute(update(m.operations).where(m.operations.c.id == "owned-control")
                           .values(error="UNCOMMITTED_POOL_SENTINEL"))
            waiter, done, results = self.start_waiter(
                lambda: check_cancelled(self.store, "owned-control", 1))
            try:
                finished_while_saturated = done.wait(2.5)
                self.assertIs(caller.get_transaction(), transaction)
                self.assertTrue(transaction.is_active)
                self.assertEqual(caller.execute(select(m.operations.c.error)).scalar_one(),
                                 "UNCOMMITTED_POOL_SENTINEL")
                self.assertEqual(self.store.engine.pool.checkedout(), 15,
                                 "The control read must not release another owner's checkout")
            finally:
                transaction.rollback()
                resources.close()
                waiter.join(timeout=3)
        self.assertFalse(waiter.is_alive(), "Owned waiter must be joined after releasing capacity")
        self.assertTrue(finished_while_saturated,
                        "Control read still waits for the default 30-second pool timeout")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], OperationControlUnavailable)
        self.assertEqual(str(results[0]), "OPERATION_CONTROL_UNAVAILABLE")
        self.assertIsNone(results[0].__cause__)
        self.assertTrue(results[0].__suppress_context__)
        self.assertEqual(self.store.engine.pool.checkedout(), 0)
        check_cancelled(self.store, "owned-control", 1)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations)).mappings().one()
            self.assertEqual((row["state"], row["cancel_requested"], row["attempt_count"], row["error"]),
                             ("RUNNING", False, 1, None))

    def test_exhausted_pool_fails_closed_and_preserves_caller_transaction(self):
        self.assert_saturated_control_is_safe()

    def test_disposed_engine_recreates_pool_with_same_wait_bound(self):
        self.store.engine.dispose()
        self.assert_saturated_control_is_safe()

    def test_capacity_return_allows_waiting_control_read_to_finish(self):
        with ExitStack() as resources:
            held = self.hold_capacity(resources)
            waiter, done, results = self.start_waiter(
                lambda: check_cancelled(self.store, "owned-control", 1))
            try:
                self.assertFalse(done.wait(.05), "Waiter should still need a connection")
                held.pop().close()
                finished_after_release = done.wait(2)
            finally:
                resources.close()
                waiter.join(timeout=3)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(finished_after_release)
        self.assertEqual(results, [None])
        self.assertEqual(self.store.engine.pool.checkedout(), 0)

    def test_general_store_checkout_also_stops_waiting_without_stealing_capacity(self):
        def checkout():
            with self.store.engine.connect() as connection:
                connection.execute(select(m.operations.c.id)).all()

        with ExitStack() as resources:
            self.hold_capacity(resources)
            waiter, done, results = self.start_waiter(checkout)
            try:
                finished_while_saturated = done.wait(2.5)
                self.assertEqual(self.store.engine.pool.checkedout(), 15)
            finally:
                resources.close()
                waiter.join(timeout=3)
        self.assertFalse(waiter.is_alive())
        self.assertTrue(finished_while_saturated, "General checkout must use the bounded wait policy")
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], PoolTimeoutError)
        self.assertEqual(self.store.engine.pool.checkedout(), 0)


class SQLitePoolWaitTests(PoolWaitContract, unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="mjobs-pool-wait-")
        self.addCleanup(self.root.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.root.name) / 'pool.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        seed_operation(self.store)

    def test_worker_reopens_blocked_state_after_pool_capacity_returns(self):
        from tests.integration.test_spine import PROFILE
        from workers.dispatch import enqueue_operation
        from workers.tasks import execute_operation

        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)
        identifier = enqueue_operation(self.store, "DISCOVER", "fixture", {"rescore_only": True})

        def saturated_control_read(worker_store, _profile, *, check, **_kwargs):
            with ExitStack() as resources:
                self.hold_capacity(resources, worker_store)
                # Unwind the fixture's held connections before worker finalization.
                check()

        with patch.dict("os.environ", {"MJOBS_DATABASE_URL": self.store.url,
                                       "MJOBS_OPERATION_MODE": "FIXTURE"}), \
                patch("workers.tasks.recalculate", side_effect=saturated_control_read):
            result = execute_operation(identifier)
        self.assertEqual(result["status"], "BLOCKED")
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(
                    m.operations.c.id == identifier)).mappings().one()
                self.assertEqual((row["state"], row["error"], row["cancel_requested"], row["attempt_count"]),
                                 ("BLOCKED", "OPERATION_CONTROL_UNAVAILABLE", False, 1))
                self.assertIsNone(row["lease_expires_at"])
                self.assertEqual(row["retry_count"], 0)
                self.assertEqual(connection.execute(select(m.matches)).all(), [])
        finally:
            reopened.engine.dispose()


class SQLiteMemoryPoolCompatibilityTests(unittest.TestCase):
    def test_private_and_uri_memory_databases_keep_identity_and_caller_transactions(self):
        urls = ["sqlite://", "sqlite:///:memory:",
                f"sqlite:///file:control-{uuid4().hex}?mode=memory&cache=shared&uri=true"]
        for url in urls:
            with self.subTest(url=url.split("?")[0]):
                store = Store(url)
                try:
                    self.assertIsInstance(store.engine.pool, SingletonThreadPool)
                    store.migrate()
                    seed_operation(store)
                    check_cancelled(store, "owned-control", 1)
                    with store.engine.connect() as caller:
                        transaction = caller.begin()
                        caller.execute(update(m.operations).values(error="MEMORY_SENTINEL"))
                        with self.assertRaises(OperationControlUnavailable):
                            check_cancelled(store, "owned-control", 1)
                        self.assertIs(caller.get_transaction(), transaction)
                        self.assertEqual(caller.execute(select(m.operations.c.error)).scalar_one(),
                                         "MEMORY_SENTINEL")
                        transaction.rollback()
                    check_cancelled(store, "owned-control", 1)
                    with store.engine.connect() as reopened:
                        self.assertIsNone(reopened.execute(select(m.operations.c.error)).scalar_one())
                finally:
                    store.engine.dispose()
