"""T11 lease bounds, ownership, expiry, attempts, and cancellation."""
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from workers.dispatch import enqueue_operation
from workers.operations import (
    MAX_ATTEMPTS, OperationCancelled, admit_inline, check_cancelled, claim,
    recover_expired, request_cancel, timeout,
)
from workers.tasks import execute_operation


class FleetT11LeaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t11-lease-")
        self.addCleanup(self.directory.cleanup)
        self.database_url = f"sqlite:///{(Path(self.directory.name) / 'operations.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.addCleanup(self.store.engine.dispose)
        self.environment = patch.dict(os.environ, {
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_OUTPUT_DIR": self.directory.name,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:8090",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def enqueue_rescore(self):
        return enqueue_operation(self.store, "DISCOVER", "t11-lease-fixture", {"rescore_only": True})

    def row(self, operation_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())

    def test_timeout_accepts_inclusive_safe_bounds(self):
        for value in ("30", "180", "600"):
            with self.subTest(value=value), patch.dict("os.environ", MJOBS_OPERATION_TIMEOUT=value):
                self.assertEqual(timeout(), int(value))

    def test_timeout_rejects_malformed_or_unbounded_values(self):
        for value in ("29", "601", "-1", "unlimited"):
            with self.subTest(value=value), patch.dict("os.environ", MJOBS_OPERATION_TIMEOUT=value):
                with self.assertRaises(ValueError):
                    timeout()

    def test_check_cancelled_rejects_expired_lease(self):
        operation_id = self.enqueue_rescore()
        claimed = claim(self.store, operation_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(
                    lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
        with self.assertRaises(OperationCancelled):
            check_cancelled(self.store, operation_id, claimed["attempt_count"])

    def test_check_cancelled_rejects_missing_lease(self):
        operation_id = self.enqueue_rescore()
        claimed = claim(self.store, operation_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(lease_expires_at=None))
        with self.assertRaises(OperationCancelled):
            check_cancelled(self.store, operation_id, claimed["attempt_count"])

    def test_check_cancelled_rejects_stale_attempt_and_accepts_live_owner(self):
        operation_id = self.enqueue_rescore()
        claimed = claim(self.store, operation_id)
        check_cancelled(self.store, operation_id, claimed["attempt_count"])
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(attempt_count=claimed["attempt_count"] + 1))
        with self.assertRaises(OperationCancelled):
            check_cancelled(self.store, operation_id, claimed["attempt_count"])

    def test_worker_rejects_expired_lease_before_acknowledging_side_effect(self):
        operation_id = self.enqueue_rescore()

        def expire_during_work(*_args, **_kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation_id).values(
                        lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))

        with patch("workers.tasks.recalculate", side_effect=expire_during_work):
            result = execute_operation(operation_id)

        self.assertEqual(result["status"], "OWNERSHIP_EXPIRED")
        row = self.row(operation_id)
        self.assertEqual(row["state"], "RUNNING")
        self.assertEqual(row["attempt_count"], 1)

    def test_worker_rejects_missing_lease_before_acknowledging_side_effect(self):
        operation_id = self.enqueue_rescore()

        def remove_lease_during_work(*_args, **_kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation_id).values(lease_expires_at=None))

        with patch("workers.tasks.recalculate", side_effect=remove_lease_during_work):
            result = execute_operation(operation_id)

        self.assertEqual(result["status"], "OWNERSHIP_EXPIRED")
        self.assertEqual(self.row(operation_id)["state"], "RUNNING")

        # A durable queued operation with a NULL lease must be reclaimed on
        # restart. The public QUEUES kinds are recoverable; the worker receives
        # a fresh bounded lease and can complete the same operation identity.
        self.store.engine.dispose()
        reopened = Store(self.database_url)
        try:
            self.assertEqual(recover_expired(reopened), 1)
            recovered = self.row(operation_id)
            self.assertEqual(recovered["state"], "QUEUED")
            self.assertEqual(recovered["error"], "LEASE_EXPIRED")
            self.assertEqual(recovered["attempt_count"], 1)
            with patch("workers.tasks.recalculate") as recalculate:
                retried = execute_operation(operation_id)
            self.assertEqual(retried["status"], "COMPLETED")
            recalculate.assert_called_once()
            completed = self.row(operation_id)
            self.assertEqual(completed["state"], "COMPLETED")
            self.assertEqual(completed["attempt_count"], 2)
        finally:
            reopened.engine.dispose()

    def test_cancelled_expired_attempt_is_terminal_after_restart(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "t11-cancel-fixture", {})
        self.assertIsNotNone(claim(self.store, operation_id))
        request_cancel(self.store, operation_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(
                    lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
        self.assertEqual(recover_expired(self.store), 1)
        self.assertEqual(self.row(operation_id)["state"], "CANCELLED")

        reopened = Store(self.database_url)
        try:
            saved = self.row(operation_id)
            self.assertEqual(saved["error"], "CANCEL_REQUESTED")
            self.assertEqual(saved["attempt_count"], 1)
            self.assertIsNone(claim(reopened, operation_id))
        finally:
            reopened.engine.dispose()

    def test_uncancelled_crashed_attempts_are_bounded(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "t11-bounded-fixture", {})
        for index in range(MAX_ATTEMPTS):
            self.assertIsNotNone(claim(self.store, operation_id))
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation_id).values(
                        lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
            self.assertEqual(recover_expired(self.store), 1)
            self.assertEqual(self.row(operation_id)["state"],
                             "QUEUED" if index < MAX_ATTEMPTS - 1 else "DEAD_LETTERED")
        saved = self.row(operation_id)
        self.assertEqual(saved["attempt_count"], MAX_ATTEMPTS)
        self.assertEqual(saved["error"], "RETRY_LIMIT")
        self.assertIsNone(claim(self.store, operation_id))

    def test_null_manual_auth_lease_remains_reserved_and_holds_browser_capacity(self):
        manual_auth = admit_inline(self.store, "MANUAL_AUTH", "t11-manual-browser")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == manual_auth).values(lease_expires_at=None))

        # NULL lease does not imply that a headed browser owner has exited.
        self.assertEqual(recover_expired(self.store), 0)
        saved = self.row(manual_auth)
        self.assertEqual(saved["state"], "RUNNING")
        self.assertEqual(saved["error"], None)
        self.assertIsNone(saved["lease_expires_at"])

        competing = enqueue_operation(
            self.store, "APPLICATION_DRY_RUN", "t11-manual-browser", {})
        self.assertIsNone(claim(self.store, competing))

    def test_null_queued_worker_lease_at_attempt_limit_honors_cancel(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "t11-null-limit-cancel", {})
        for attempt in range(MAX_ATTEMPTS):
            claimed = claim(self.store, operation_id)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["attempt_count"], attempt + 1)
            if attempt < MAX_ATTEMPTS - 1:
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(
                        m.operations.c.id == operation_id).values(
                            lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
                self.assertEqual(recover_expired(self.store), 1)
                self.assertEqual(self.row(operation_id)["state"], "QUEUED")
                continue

            self.assertTrue(request_cancel(self.store, operation_id)["cancel_requested"])
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation_id).values(lease_expires_at=None))
            self.assertEqual(recover_expired(self.store), 1)

        saved = self.row(operation_id)
        self.assertEqual(saved["state"], "CANCELLED")
        self.assertEqual(saved["error"], "CANCEL_REQUESTED")
        self.assertEqual(saved["attempt_count"], MAX_ATTEMPTS)
        self.assertIsNone(claim(self.store, operation_id))


if __name__ == "__main__":
    unittest.main()
