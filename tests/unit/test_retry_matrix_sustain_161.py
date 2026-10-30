"""MJ-REQ-160: offline queue outage/reopen/reconnect regression evidence.

Real SQLite outbox, dispatcher and worker lifecycle; synthetic Redis/RQ and
collector boundaries only. This does not certify real Redis, concurrent workers,
browser/AI retries or exactly-once external submissions. Requirement is PARTIAL.
"""
from importlib import import_module
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from redis.exceptions import ConnectionError as RedisConnectionError
from rq.exceptions import NoSuchJobError
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source
from workers.dispatch import dispatch_pending, enqueue_operation
from workers.tasks import execute_operation


class QueueReconnectTests(unittest.TestCase):
    def test_outage_reopen_reconnect_and_duplicate_delivery_keep_one_execution(self):
        for failure_point in ("ping", "before_enqueue", "after_enqueue"):
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as directory:
                self.exercise_outage(Path(directory), failure_point)

    def exercise_outage(self, root, failure_point):
        url = f"sqlite:///{(root / 'retry.db').as_posix()}"
        store = Store(url)
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile({"id": "fixture", "name": "Retry Fixture",
                            "email": "retry@example.test", "answers": [], "facts": []})
        source = configure_source(store, "offline-reconnect")
        params = {"source_id": source}
        identifier = enqueue_operation(store, "DISCOVER", "fixture", params)
        self.assertEqual(enqueue_operation(store, "DISCOVER", "fixture", params), identifier)
        jobs, deliveries = {}, []
        connected = False
        redis = Mock()

        def ping():
            if not connected and failure_point == "ping":
                raise RedisConnectionError("synthetic outage")
            return True

        def fetch(job_id, *, connection):
            self.assertIs(connection, redis)
            if job_id not in jobs:
                raise NoSuchJobError(job_id)
            return jobs[job_id]

        test = self

        class SyntheticQueue:
            def __init__(self, name, connection):
                test.assertIs(connection, redis)
                self.name = name

            def enqueue(self, function, *args, **options):
                if not connected and failure_point == "before_enqueue":
                    raise RedisConnectionError("synthetic outage")
                job_id = options["job_id"]
                # Deliberately do not deduplicate here: production dispatcher
                # Job.fetch/status protection must prevent a second enqueue.
                deliveries.append((function, args, options.copy()))
                jobs[job_id] = Mock()
                jobs[job_id].get_status.return_value = "queued"
                if not connected and failure_point == "after_enqueue":
                    raise RedisConnectionError("synthetic lost enqueue acknowledgment")

        def row():
            with store.engine.connect() as connection:
                rows = list(connection.execute(select(m.operations)).mappings())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], identifier)
            self.assertEqual(rows[0]["kind"], "DISCOVER")
            self.assertEqual(rows[0]["entity_id"], "fixture")
            self.assertEqual(rows[0]["params"], params)
            return dict(rows[0])

        def reopen():
            nonlocal store
            store.engine.dispose()
            store = Store(url)

        redis.ping.side_effect = ping
        environment = {"MJOBS_DATABASE_URL": url, "MJOBS_OPERATION_MODE": "FIXTURE",
                       "MJOBS_OUTPUT_DIR": str(root)}
        try:
            with patch.dict(os.environ, environment), \
                    patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
                    patch("socket.socket.connect_ex", side_effect=AssertionError("network forbidden")), \
                    patch("workers.dispatch.Redis.from_url", return_value=redis), \
                    patch("workers.dispatch.Job.fetch", side_effect=fetch), \
                    patch("workers.dispatch.Queue", SyntheticQueue), \
                    patch("workers.tasks.collect_source", return_value={"jobs_seen": 0}) as collector:
                initial = row()
                self.assertEqual(dispatch_pending(store, "redis://synthetic.invalid/0"),
                                 {"status": "QUEUE_UNAVAILABLE", "count": 0})
                redis.close.assert_called_once_with()
                collector.assert_not_called()
                self.assertEqual(row(), initial)
                reopen()
                self.assertEqual(row(), initial)
                self.assertEqual(row()["state"], "QUEUED")
                self.assertEqual(row()["attempt_count"], 0)
                self.assertEqual(row()["retry_count"], 0)
                self.assertIsNone(row()["next_attempt_at"])
                self.assertEqual(enqueue_operation(store, "DISCOVER", "fixture", params), identifier)

                connected = True
                expected = 0 if failure_point == "after_enqueue" else 1
                self.assertEqual(dispatch_pending(store, "redis://synthetic.invalid/0"),
                                 {"status": "DISPATCHED", "count": expected})
                self.assertEqual(dispatch_pending(store, "redis://synthetic.invalid/0"),
                                 {"status": "DISPATCHED", "count": 0})
                self.assertEqual(len(deliveries), 1)
                function, args, options = deliveries[0]
                self.assertEqual(function, "workers.tasks.execute_operation")
                self.assertEqual(args, (identifier,))
                self.assertEqual(options["job_id"], identifier)
                self.assertGreater(options["job_timeout"], 0)
                collector.assert_not_called()
                module, name = function.rsplit(".", 1)
                self.assertEqual(getattr(import_module(module), name)(*args)["status"], "COMPLETED")
                collector.assert_called_once()
                self.assertEqual(collector.call_args.args[1:3], (source, "fixture"))
                completed = row()
                self.assertEqual(completed["state"], "COMPLETED")
                self.assertEqual(completed["attempt_count"], 1)
                self.assertEqual(completed["retry_count"], 0)
                self.assertIsNotNone(completed["completed_at"])

                # Simulate loss of queue metadata after completion and a stale
                # duplicate delivery. SQLite state, not the fake queue, fences it.
                jobs.clear()
                reopen()
                self.assertEqual(row(), completed)
                self.assertEqual(execute_operation(identifier)["status"], "ALREADY_CLAIMED_OR_CAPACITY")
                self.assertEqual(dispatch_pending(store, "redis://synthetic.invalid/0"),
                                 {"status": "DISPATCHED", "count": 0})
                collector.assert_called_once()
                self.assertEqual(len(deliveries), 1)
                self.assertEqual(row(), completed)
                self.assertEqual(redis.close.call_count, 4)
                with store.engine.connect() as connection:
                    actions = list(connection.execute(select(m.audit.c.action).where(
                        m.audit.c.entity_id == identifier)).scalars())
                self.assertEqual(actions.count("OPERATION_ATTEMPT_STARTED"), 1)
                self.assertEqual(actions.count("OPERATION_ATTEMPT_FINISHED"), 1)
                self.assertNotIn("OPERATION_RETRY_SCHEDULED", actions)
        finally:
            store.engine.dispose()
