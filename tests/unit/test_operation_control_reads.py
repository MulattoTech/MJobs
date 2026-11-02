"""Real SQLite lock contention cannot block the durable control read for 15s."""
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, now
from workers.operations import OperationControlUnavailable, check_cancelled


def seed_operation(store):
    with store.engine.begin() as connection:
        connection.execute(m.operations.insert().values(
            id="owned-control", kind="DISCOVER", entity_id="synthetic", params={},
            state="RUNNING", attempt_count=1, updated_at=now(),
            lease_expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()))


class OperationControlReadTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="mjobs-control-lock-")
        self.addCleanup(self.root.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.root.name) / 'control.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        seed_operation(self.store)

    def test_actual_exclusive_lock_is_bounded_sanitized_and_releases_checkout(self):
        check_cancelled(self.store, "owned-control", 1)
        with self.store.engine.connect() as blocker:
            blocker.exec_driver_sql("BEGIN EXCLUSIVE")
            started = time.monotonic()
            with self.assertRaises(OperationControlUnavailable) as failure:
                check_cancelled(self.store, "owned-control", 1)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, .06, "The real database read did not encounter the held lock")
            self.assertLess(elapsed, 1, "Control read inherited SQLite's 15-second general timeout")
            self.assertEqual(str(failure.exception), "OPERATION_CONTROL_UNAVAILABLE")
            self.assertIsNone(failure.exception.__cause__)
            self.assertEqual(self.store.engine.pool.checkedout(), 1, "Only the fixture lock may remain checked out")
        check_cancelled(self.store, "owned-control", 1)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one(), 15000)
            row = connection.execute(select(m.operations)).mappings().one()
            self.assertEqual((row["state"], row["attempt_count"], row["cancel_requested"]), ("RUNNING", 1, False))
        self.assertEqual(self.store.engine.pool.checkedout(), 0)

    def test_stricter_sqlite_connection_timeout_is_preserved(self):
        with self.store.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA busy_timeout = 20")
        check_cancelled(self.store, "owned-control", 1)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one(), 20)

    def test_shared_memory_connection_never_rolls_back_another_callers_write(self):
        shared = Store("sqlite:///:memory:")
        try:
            shared.migrate()
            seed_operation(shared)
            with shared.engine.connect() as caller:
                transaction = caller.begin()
                caller.execute(update(m.operations).where(m.operations.c.id == "owned-control")
                               .values(error="UNCOMMITTED_SENTINEL"))
                with self.assertRaises(OperationControlUnavailable):
                    check_cancelled(shared, "owned-control", 1)
                self.assertIs(caller.get_transaction(), transaction)
                self.assertTrue(caller.connection.driver_connection.in_transaction)
                self.assertEqual(caller.execute(select(m.operations.c.error)).scalar_one(), "UNCOMMITTED_SENTINEL")
                transaction.rollback()
            check_cancelled(shared, "owned-control", 1)
            with shared.engine.connect() as connection:
                self.assertIsNone(connection.execute(select(m.operations.c.error)).scalar_one())
        finally:
            shared.engine.dispose()

    def test_worker_persists_unavailable_as_blocked_without_claiming_cancellation(self):
        from tests.integration.test_spine import PROFILE
        from workers.dispatch import enqueue_operation
        from workers.tasks import execute_operation

        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)
        identifier = enqueue_operation(self.store, "DISCOVER", "fixture", {"rescore_only": True})
        def actual_locked_read(_store, _profile, *, check, **_kwargs):
            with self.store.engine.connect() as blocker:
                blocker.exec_driver_sql("BEGIN EXCLUSIVE")
                check()

        with patch.dict("os.environ", {"MJOBS_DATABASE_URL": self.store.url, "MJOBS_OPERATION_MODE": "FIXTURE"}), \
                patch("workers.tasks.recalculate", side_effect=actual_locked_read):
            result = execute_operation(identifier)
        self.assertEqual(result["status"], "BLOCKED")
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(m.operations.c.id == identifier)).mappings().one()
                self.assertEqual((row["state"], row["error"], row["cancel_requested"], row["attempt_count"]),
                                 ("BLOCKED", "OPERATION_CONTROL_UNAVAILABLE", False, 1))
                self.assertIsNone(row["lease_expires_at"])
                self.assertEqual(connection.execute(select(m.matches)).all(), [])
        finally:
            reopened.engine.dispose()

    def test_source_worker_keeps_actual_control_lock_failure_out_of_provider_health(self):
        from services.discovery.sources.collect import configure_source
        from tests.integration.test_spine import PROFILE
        from workers.dispatch import enqueue_operation
        from workers.tasks import execute_operation

        self.store.save_profile(PROFILE)
        source_id = configure_source(self.store, 'fixture')
        identifier = enqueue_operation(self.store, 'DISCOVER', 'fixture', {'source_id': source_id})

        def actual_locked_read(adapter):
            with self.store.engine.connect() as blocker:
                blocker.exec_driver_sql('BEGIN EXCLUSIVE')
                adapter.check()

        with patch.dict('os.environ', {'MJOBS_DATABASE_URL': self.store.url, 'MJOBS_OPERATION_MODE': 'FIXTURE'}), \
                patch('services.discovery.sources.greenhouse.Greenhouse.search',
                      autospec=True, side_effect=actual_locked_read):
            result = execute_operation(identifier)
        self.assertEqual(result['status'], 'BLOCKED', result)
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == identifier)).mappings().one()
        self.assertEqual(source['health_status'], 'NOT_CHECKED')
        self.assertEqual(source['consecutive_failures'], 0)
        self.assertIsNone(source['backoff_until'])
        self.assertIsNone(source['last_error'])
        self.assertEqual(operation['error'], 'OPERATION_CONTROL_UNAVAILABLE')
        self.assertFalse(operation['cancel_requested'])
        self.assertEqual(operation['retry_count'], 0)
        self.assertEqual(self.store.count('source_quality_observations'), 0)
