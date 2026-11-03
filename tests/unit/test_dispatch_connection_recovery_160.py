"""MJ-REQ-160: Redis dispatcher failures remain retryable without replaying work.

Synthetic Redis and real disposable SQLite outbox; no external sockets or tasks.
"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from redis.exceptions import ConnectionError as RedisConnectionError
from rq.exceptions import NoSuchJobError
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from workers.dispatch import dispatch_pending, enqueue_operation


class DispatcherConnectionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'operations.db').as_posix()}")
        self.store.migrate()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def state(self, operation_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())

    def test_connection_factory_redis_outage_preserves_durable_queue(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        initial = self.state(operation_id)
        with patch("workers.dispatch.Redis.from_url",
                   side_effect=RedisConnectionError("synthetic constructor outage")), \
             patch("workers.dispatch.Queue") as queue:
            self.assertEqual(
                dispatch_pending(self.store, "redis://synthetic.invalid/0"),
                {"status": "QUEUE_UNAVAILABLE", "count": 0},
            )
            queue.assert_not_called()
        self.assertEqual(self.state(operation_id), initial)
        self.assertEqual(initial["state"], "QUEUED")
        self.assertEqual(initial["attempt_count"], 0)

    def test_close_failure_does_not_mask_acknowledged_dispatch_or_duplicate(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        redis = Mock()
        redis.ping.return_value = True
        redis.close.side_effect = RedisConnectionError("synthetic close outage")
        jobs, deliveries = {}, []
        test = self

        def fetch(job_id, *, connection):
            test.assertIs(connection, redis)
            if job_id not in jobs:
                raise NoSuchJobError(job_id)
            return jobs[job_id]

        class SyntheticQueue:
            def __init__(self, name, connection):
                test.assertIs(connection, redis)

            def enqueue(self, function, *args, **options):
                deliveries.append((function, args, options["job_id"]))
                job = Mock()
                job.get_status.return_value = "queued"
                jobs[options["job_id"]] = job

        with patch("workers.dispatch.Redis.from_url", return_value=redis), \
             patch("workers.dispatch.Job.fetch", side_effect=fetch), \
             patch("workers.dispatch.Queue", SyntheticQueue):
            self.assertEqual(
                dispatch_pending(self.store, "redis://synthetic.invalid/0"),
                {"status": "DISPATCHED", "count": 1},
            )
            self.assertEqual(
                dispatch_pending(self.store, "redis://synthetic.invalid/0"),
                {"status": "DISPATCHED", "count": 0},
            )
        self.assertEqual(deliveries, [
            ("workers.tasks.execute_operation", (operation_id,), operation_id),
        ])
        self.assertEqual(redis.close.call_count, 2)
        self.assertEqual(self.state(operation_id)["state"], "QUEUED")
        self.assertEqual(self.state(operation_id)["attempt_count"], 0)

    def test_invalid_connection_configuration_is_not_a_transient_outage(self):
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        initial = self.state(operation_id)
        with patch("workers.dispatch.Redis.from_url",
                   side_effect=ValueError("invalid local Redis URL")):
            with self.assertRaisesRegex(ValueError, "invalid local Redis URL"):
                dispatch_pending(self.store, "not-a-redis-url")
        self.assertEqual(self.state(operation_id), initial)


if __name__ == "__main__":
    unittest.main()
