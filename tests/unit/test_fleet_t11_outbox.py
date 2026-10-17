"""Team 11 regression evidence for durable outbox recovery."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from rq.job import JobStatus
from workers.dispatch import dispatch_pending, enqueue_operation
from workers.operations import (
    OperationCancelled,
    check_cancelled,
    claim,
    request_cancel,
)


class FleetT11OutboxTests(unittest.TestCase):
    def setUp(self):
        self.operation_timeout_patch = patch.dict(
            os.environ, {"MJOBS_OPERATION_TIMEOUT": "180"})
        self.operation_timeout_patch.start()
        self.addCleanup(self.operation_timeout_patch.stop)
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t11-outbox-")
        database = Path(self.directory.name) / "outbox.db"
        self.store = Store(f"sqlite:///{database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def read(self, operation_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())

    def expire(self, operation_id):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(lease_expires_at=(
                    datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))

    def test_recovered_operation_waits_for_rq_started_delivery_cleanup(self):
        """A stale RQ started status is skipped; recovery does not delete it."""
        operation_id = enqueue_operation(
            self.store, "DISCOVER", "fixture", {"rescore_only": True})
        self.assertIsNotNone(claim(self.store, operation_id))
        self.expire(operation_id)

        redis = Mock()
        redis.ping.return_value = True
        stale_job = Mock()
        stale_job.get_status.return_value = JobStatus.STARTED
        queue = Mock()

        with patch("workers.dispatch.Redis.from_url", return_value=redis), \
                patch("workers.dispatch.Job.fetch", return_value=stale_job), \
                patch("workers.dispatch.Queue", return_value=queue):
            result = dispatch_pending(self.store, "redis://unused/0")

        self.assertEqual(result, {"status": "DISPATCHED", "count": 0})
        recovered = self.read(operation_id)
        self.assertEqual((recovered["state"], recovered["error"],
                          recovered["attempt_count"]),
                         ("QUEUED", "LEASE_EXPIRED", 1))
        queue.enqueue.assert_not_called()
        stale_job.delete.assert_not_called()
        redis.close.assert_called_once_with()

    def test_failed_rq_delivery_redispatches_identity_once_and_fences_old_attempt(self):
        operation_id = enqueue_operation(
            self.store, "DISCOVER", "fixture", {"rescore_only": True})
        first_owner = claim(self.store, operation_id)
        self.expire(operation_id)

        redis = Mock()
        redis.ping.return_value = True
        failed_delivery = Mock()
        failed_delivery.get_status.return_value = JobStatus.FAILED
        queued_delivery = Mock()
        queued_delivery.get_status.return_value = JobStatus.QUEUED
        queue = Mock()

        with patch("workers.dispatch.Redis.from_url", return_value=redis), \
                patch("workers.dispatch.Job.fetch",
                      side_effect=[failed_delivery, queued_delivery]) as fetch, \
                patch("workers.dispatch.Queue", return_value=queue) as queue_type:
            first = dispatch_pending(self.store, "redis://unused/0")
            second = dispatch_pending(self.store, "redis://unused/0")

        self.assertEqual(first, {"status": "DISPATCHED", "count": 1})
        self.assertEqual(second, {"status": "DISPATCHED", "count": 0})
        self.assertEqual(fetch.call_count, 2)
        failed_delivery.delete.assert_called_once_with()
        queued_delivery.delete.assert_not_called()
        queue_type.assert_called_once()
        queue.enqueue.assert_called_once_with(
            "workers.tasks.execute_operation", operation_id,
            job_id=operation_id, job_timeout=180,
            result_ttl=86400, failure_ttl=86400)

        new_owner = claim(self.store, operation_id)
        self.assertEqual(new_owner["attempt_count"], first_owner["attempt_count"] + 1)
        with self.assertRaises(OperationCancelled):
            check_cancelled(self.store, operation_id, first_owner["attempt_count"])
        check_cancelled(self.store, operation_id, new_owner["attempt_count"])

    def test_recovered_cancelled_operation_is_never_redispatched(self):
        operation_id = enqueue_operation(
            self.store, "DISCOVER", "fixture", {"rescore_only": True})
        self.assertIsNotNone(claim(self.store, operation_id))
        self.assertTrue(request_cancel(self.store, operation_id)["cancel_requested"])
        self.expire(operation_id)

        redis = Mock()
        redis.ping.return_value = True
        with patch("workers.dispatch.Redis.from_url", return_value=redis), \
                patch("workers.dispatch.Job.fetch") as fetch, \
                patch("workers.dispatch.Queue") as queue_type:
            result = dispatch_pending(self.store, "redis://unused/0")

        self.assertEqual(result, {"status": "DISPATCHED", "count": 0})
        saved = self.read(operation_id)
        self.assertEqual((saved["state"], saved["error"], saved["attempt_count"]),
                         ("CANCELLED", "CANCEL_REQUESTED", 1))
        fetch.assert_not_called()
        queue_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
