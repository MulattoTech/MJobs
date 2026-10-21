"""T11 Redis/RQ dispatch recovery across the enqueue/SQL-commit seam.

Set MJOBS_T11_REDIS_URL to an isolated Redis database to run this integration
test. The operation database is a temporary SQLite file; PostgreSQL crash
recovery is covered by the existing worker lifecycle acceptance suite.
"""
import os
import ipaddress
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch
from uuid import uuid4

from redis import Redis
from fastapi.testclient import TestClient
from rq import Queue
from rq.job import Job, JobStatus
from rq.exceptions import NoSuchJobError
from rq.registry import FailedJobRegistry, StartedJobRegistry
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from apps.api.main import create_app
from tests.integration.test_spine import PROFILE
from workers.dispatch import dispatch_pending, enqueue_operation
from workers.operations import QUEUES, claim, recover_expired
from workers.tasks import execute_operation


REDIS_URL = os.environ.get("MJOBS_T11_REDIS_URL")


def is_admitted_disposable_redis_target(url):
    """Only permit an explicitly selected loopback Redis database above DB 0."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        database = parsed.path.strip("/")
        if parsed.scheme not in {"redis", "rediss"} or not host or not database.isdigit():
            return False
        if (parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            return False
        if parsed.netloc.rsplit("@", 1)[-1].endswith(":"):
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        if int(database) < 1:
            return False
        if host.lower() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


class FleetT11SqliteDispatcherRecoveryTests(unittest.TestCase):
    """Exercise the public outbox/dispatcher recovery flow without services."""

    def test_real_redis_target_admission_requires_loopback_nonzero_database(self):
        self.assertTrue(is_admitted_disposable_redis_target("redis://127.0.0.1:6379/1"))
        self.assertTrue(is_admitted_disposable_redis_target("redis://localhost:6379/2"))
        for unsafe in (
                "redis://example.com:6379/1", "redis://192.0.2.8:6379/1",
                "redis://127.0.0.1:6379/0", "redis://127.0.0.1:6379/",
                "http://127.0.0.1:6379/1", "redis://user@127.0.0.1:6379/1",
                "redis://:secret@127.0.0.1:6379/1", "redis://127.0.0.1:6379/1?ssl=true",
                "redis://127.0.0.1:6379/1#fragment", "redis://127.0.0.1:bad/1",
                "redis://127.0.0.1:/1", "redis://127.0.0.1:0/1",
                "redis://127.0.0.1:65536/1"):
            with self.subTest(url=unsafe):
                self.assertFalse(is_admitted_disposable_redis_target(unsafe))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t11-outbox-")
        self.addCleanup(self.directory.cleanup)
        self.database_url = f"sqlite:///{(Path(self.directory.name) / 'operations.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.addCleanup(self.store.engine.dispose)

    def test_enqueue_survives_dispatch_interrupt_restart_and_readback(self):
        jobs = {}
        redis = type("SyntheticRedis", (), {
            "ping": lambda _self: True,
            "close": lambda _self: None,
        })()

        class SyntheticJob:
            def __init__(self, identifier, origin, function, args):
                self.id = identifier
                self.origin = origin
                self.func_name = function
                self.args = args

            def get_status(self):
                return "queued"

            def delete(self):
                jobs.pop(self.id, None)

            @classmethod
            def fetch(cls, identifier, connection=None):
                if identifier not in jobs:
                    raise NoSuchJobError(identifier)
                return jobs[identifier]

        class SyntheticQueue:
            def __init__(self, name, connection=None):
                self.name = name

            def enqueue(self, function, operation_id, *, job_id, **_kwargs):
                job = SyntheticJob(job_id, self.name, function, (operation_id,))
                jobs[job_id] = job
                if not interrupted["value"]:
                    interrupted["value"] = True
                    raise RuntimeError("synthetic dispatcher interruption")
                return job

        interrupted = {"value": False}
        operation_id = enqueue_operation(
            self.store, "DISCOVER", "t11-sqlite-recovery-" + uuid4().hex, {})

        with patch("workers.dispatch.Redis.from_url", return_value=redis), \
                patch("workers.dispatch.Queue", SyntheticQueue), \
                patch("workers.dispatch.Job", SyntheticJob):
            with self.assertRaisesRegex(RuntimeError, "synthetic dispatcher interruption"):
                dispatch_pending(self.store, "redis://synthetic/0")

            # Read the committed outbox row from a newly opened Store after
            # the dispatch transaction rolled back around the synthetic crash.
            reopened = Store(self.database_url)
            try:
                with reopened.engine.connect() as connection:
                    row = connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id)).mappings().one()
                self.assertEqual(row["state"], "QUEUED")
                self.assertEqual(row["attempt_count"], 0)

                result = dispatch_pending(reopened, "redis://synthetic/0")
                self.assertEqual(result, {"status": "DISPATCHED", "count": 0})
                self.assertEqual(len(jobs), 1)
                job = jobs[operation_id]
                self.assertEqual(job.origin, QUEUES["DISCOVER"])
                self.assertEqual(job.func_name, "workers.tasks.execute_operation")
                self.assertEqual(job.args, (operation_id,))
                self.assertEqual(enqueue_operation(
                    reopened, "DISCOVER", row["entity_id"], {}), operation_id)
                self.assertEqual(reopened.count("worker_operations"), 1)
            finally:
                reopened.engine.dispose()


class FleetT11SqlitePublicFlowTests(unittest.TestCase):
    """Public API, worker, restart and readback flow using synthetic work."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t11-public-")
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
        self.app = create_app(
            self.store, "http://127.0.0.1:8090", self.directory.name,
            operation_mode="FIXTURE")

    def client(self):
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def admit_profile(self, client):
        response = client.put("/profiles", json=PROFILE)
        self.assertEqual(response.status_code, 200, response.text)

    def queue_discovery(self, client):
        response = client.post("/discovery", json={
            "profile_id": PROFILE["id"], "request_id": "t11-" + uuid4().hex,
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["operation_id"]

    def test_api_discovery_expiry_restart_retry_and_operation_readback(self):
        client = self.client()
        self.admit_profile(client)
        operation_id = self.queue_discovery(client)
        self.assertEqual(client.get(f"/operations/{operation_id}").json()["state"], "QUEUED")
        calls = {"count": 0}

        def synthetic_recalculate(store, _profile_id, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                with store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(
                        m.operations.c.id == operation_id).values(
                            lease_expires_at="2000-01-01T00:00:00+00:00"))

        def synthetic_discover(store, _origin, profile_id, **kwargs):
            synthetic_recalculate(store, profile_id, **kwargs)
            return []

        with patch("workers.tasks.recalculate", side_effect=synthetic_recalculate), \
                patch("workers.tasks.discover", side_effect=synthetic_discover):
            interrupted = execute_operation(operation_id)
            first_readback = client.get(f"/operations/{operation_id}").json()
            self.assertEqual(interrupted["status"], "OWNERSHIP_EXPIRED")
            self.assertEqual(first_readback["state"], "RUNNING")

            # A new Store simulates API/worker process restart. Recovery leaves
            # the durable operation retryable, and the second synthetic worker
            # completes it without Redis, PostgreSQL, or provider traffic.
            reopened = Store(self.database_url)
            reopened_app = create_app(
                reopened, "http://127.0.0.1:8090", self.directory.name,
                operation_mode="FIXTURE")
            try:
                with TestClient(reopened_app) as restarted_client:
                    self.assertEqual(recover_expired(reopened), 1)
                    self.assertEqual(restarted_client.get(
                        f"/operations/{operation_id}").json()["state"], "QUEUED")
                    resumed = execute_operation(operation_id)
                    self.assertEqual(resumed["status"], "COMPLETED")
                    final = restarted_client.get(f"/operations/{operation_id}").json()
                    self.assertEqual(final["state"], "COMPLETED")
                    self.assertEqual(final["attempt_count"], 2)
                    self.assertEqual(reopened.count("worker_operations"), 1)
            finally:
                reopened.engine.dispose()

    def test_public_cancel_control_prevents_worker_execution(self):
        client = self.client()
        self.admit_profile(client)
        operation_id = self.queue_discovery(client)
        cancelled = client.post(f"/operations/{operation_id}/cancel")
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        self.assertEqual(cancelled.json()["state"], "CANCELLED")
        self.assertEqual(client.get(f"/operations/{operation_id}").json()["state"], "CANCELLED")

        with patch("workers.tasks.discover", side_effect=AssertionError("cancelled work must not run")):
            result = execute_operation(operation_id)
        self.assertEqual(result["status"], "ALREADY_CLAIMED_OR_CAPACITY")
        self.assertEqual(client.get(f"/operations/{operation_id}").json()["attempt_count"], 0)


@unittest.skipUnless(
    is_admitted_disposable_redis_target(REDIS_URL),
    "Set MJOBS_T11_REDIS_URL to a loopback Redis database numbered 1 or higher",
)
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
        # Remove only UUID-keyed RQ jobs created by this test.
        for job_id in (self.operation_id, getattr(self, "unrelated_job_id", None)):
            if not job_id:
                continue
            try:
                job = Job.fetch(job_id, connection=self.redis)
            except NoSuchJobError:
                continue
            Queue(job.origin, connection=self.redis).remove(job_id)
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

    def test_rq_stale_started_maintenance_allows_stable_id_redelivery(self):
        queue = Queue(QUEUES["DISCOVER"], connection=self.redis)
        started = StartedJobRegistry(queue=queue)
        failed = FailedJobRegistry(queue=queue)

        # Start with a real queued RQ job and a durable claimed operation.
        self.assertEqual(dispatch_pending(self.store, REDIS_URL),
                         {"status": "DISPATCHED", "count": 1})
        self.assertEqual(claim(self.store, self.operation_id)["attempt_count"], 1)
        job = Job.fetch(self.operation_id, connection=self.redis)
        queue.remove(job.id)
        job.set_status(JobStatus.STARTED)
        job.save()
        started.add(job)

        # Age only our StartedJobRegistry member. Keep a second, fresh started
        # job as a negative control; RQ maintenance must not clean it.
        cutoff = time.time() - 60
        self.unrelated_job_id = "t11-rq-unrelated-" + uuid4().hex
        unrelated = queue.enqueue(
            "workers.tasks.execute_operation", self.unrelated_job_id,
            job_id=self.unrelated_job_id)
        queue.remove(unrelated.id)
        unrelated.set_status(JobStatus.STARTED)
        unrelated.save()
        started.add(unrelated)
        self.redis.zadd(started.key, {job.id: cutoff - 1})
        self.redis.zadd(started.key, {unrelated.id: cutoff + 60})

        # Mark the SQL lease expired, as after a worker crash. The RQ cleanup
        # call below is the actual maintenance API, not a mocked transition.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id).values(
                    lease_expires_at="2000-01-01T00:00:00+00:00"))

        stale_members = self.redis.zrangebyscore(started.key, "-inf", cutoff)
        self.assertEqual(stale_members, [self.operation_id.encode()])
        started.cleanup(timestamp=cutoff)

        self.assertEqual(Job.fetch(self.operation_id, connection=self.redis).get_status(), "failed")
        self.assertNotIn(self.operation_id, started.get_job_ids(cleanup=False))
        self.assertIn(self.operation_id, failed.get_job_ids(cleanup=False))
        self.assertIn(self.unrelated_job_id, started.get_job_ids(cleanup=False))
        self.assertEqual(Job.fetch(self.unrelated_job_id, connection=self.redis).get_status(), "started")

        # The production dispatcher observes expired SQL ownership, recovers
        # the durable row, removes the failed RQ record, and redelivers exactly
        # once using the original stable operation ID.
        reopened = Store(self.database_url)
        try:
            self.assertEqual(dispatch_pending(reopened, REDIS_URL),
                             {"status": "DISPATCHED", "count": 1})
            redelivered = Job.fetch(self.operation_id, connection=self.redis)
            self.assertEqual(redelivered.id, self.operation_id)
            self.assertEqual(redelivered.get_status(), "queued")
            self.assertEqual(redelivered.origin, QUEUES["DISCOVER"])
            self.assertEqual(redelivered.args, (self.operation_id,))
            self.assertNotIn(self.operation_id, failed.get_job_ids(cleanup=False))
            self.assertNotIn(self.operation_id, started.get_job_ids(cleanup=False))
            self.assertIn(self.unrelated_job_id, started.get_job_ids(cleanup=False))
            self.assertEqual(queue.get_job_ids().count(self.operation_id), 1)

            with reopened.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(
                    m.operations.c.id == self.operation_id)).mappings().one()
            self.assertEqual(row["state"], "QUEUED")
            self.assertEqual(row["attempt_count"], 1)
            self.assertEqual(row["retry_count"], 1)

            self.assertEqual(dispatch_pending(reopened, REDIS_URL),
                             {"status": "DISPATCHED", "count": 0})
            self.assertEqual(queue.get_job_ids().count(self.operation_id), 1)
            with reopened.engine.connect() as connection:
                final = connection.execute(select(
                    m.operations.c.state, m.operations.c.attempt_count,
                    m.operations.c.retry_count).where(
                        m.operations.c.id == self.operation_id)).one()
            self.assertEqual((final.state, final.attempt_count, final.retry_count),
                             ("QUEUED", 1, 1))
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
