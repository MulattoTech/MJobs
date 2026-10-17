"""Team 11 regression evidence for durable outbox recovery."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from rq.job import JobStatus
from workers.dispatch import dispatch_pending, enqueue_operation
from workers.operations import claim


class FleetT11OutboxTests(unittest.TestCase):
    def setUp(self):
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

    def test_recovered_operation_waits_for_rq_started_delivery_cleanup(self):
        """A stale RQ started status is skipped; recovery does not delete it."""
        operation_id = enqueue_operation(
            self.store, "DISCOVER", "fixture", {"rescore_only": True})
        self.assertIsNotNone(claim(self.store, operation_id))
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(lease_expires_at=(
                    datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))

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


if __name__ == "__main__":
    unittest.main()
