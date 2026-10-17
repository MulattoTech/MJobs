"""T11 Redis/RQ dispatch recovery across the enqueue/SQL-commit seam.

Set MJOBS_T11_REDIS_URL to an isolated Redis database to run this integration
test. The operation database is a temporary SQLite file; PostgreSQL crash
recovery is covered by the existing worker lifecycle acceptance suite.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from redis import Redis
from rq import Queue
from rq.job import Job
from rq.exceptions import NoSuchJobError
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from workers.dispatch import dispatch_pending, enqueue_operation
from workers.operations import QUEUES


REDIS_URL = os.environ.get("MJOBS_T11_REDIS_URL")


@unittest.skipUnless(REDIS_URL, "Set MJOBS_T11_REDIS_URL to an isolated Redis database")
class FleetT11RqRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t11-rq-")
        self.addCleanup(self.directory.cleanup)
        self.database_url = f"sqlite:///{(Path(self.directory.name) / 'operations.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.addCleanup(self.store.engine.dispose)
        self.redis = Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        self.redis.ping()
        self.addCleanup(self.redis.close)
        entity_id = "t11-rq-recovery-" + uuid4().hex
        self.operation_id = enqueue_operation(self.store, "DISCOVER", entity_id, {})

    def tearDown(self):
        # Remove only this test's UUID-keyed RQ job from the shared test queue.
        queue = Queue(QUEUES["DISCOVER"], connection=self.redis)
        try:
            job = Job.fetch(self.operation_id, connection=self.redis)
        except NoSuchJobError:
            return
        queue.remove(self.operation_id)
        job.delete()

    def test_queued_rq_job_survives_dispatcher_interruption_after_redis_write(self):
        original_enqueue = Queue.enqueue

        def enqueue_then_interrupt(queue, *args, **kwargs):
            job = original_enqueue(queue, *args, **kwargs)
            if kwargs.get("job_id") == self.operation_id:
                raise RuntimeError("synthetic dispatcher interruption")
            return job

        with patch("workers.dispatch.Queue.enqueue", new=enqueue_then_interrupt):
            with self.assertRaisesRegex(RuntimeError, "synthetic dispatcher interruption"):
                dispatch_pending(self.store, REDIS_URL)

        # Redis has the delivery, but the outbox transaction remains readable
        # as queued work after a fresh SQL connection is opened.
        reopened = Store(self.database_url)
        try:
            with reopened.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(
                    m.operations.c.id == self.operation_id)).mappings().one()
            self.assertEqual(row["state"], "QUEUED")
            self.assertEqual(row["attempt_count"], 0)

            job = Job.fetch(self.operation_id, connection=self.redis)
            self.assertEqual(job.get_status(), "queued")
            self.assertEqual(job.origin, QUEUES["DISCOVER"])
            self.assertEqual(job.args, (self.operation_id,))

            # A restarted dispatcher sees the same stable RQ job ID and does
            # not create a second delivery or consume a worker attempt.
            result = dispatch_pending(reopened, REDIS_URL)
            self.assertEqual(result, {"status": "DISPATCHED", "count": 0})
            self.assertEqual(Job.fetch(self.operation_id, connection=self.redis).id,
                             self.operation_id)
            with reopened.engine.connect() as connection:
                persisted = connection.execute(select(m.operations.c.attempt_count).where(
                    m.operations.c.id == self.operation_id)).scalar_one()
            self.assertEqual(persisted, 0)
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
