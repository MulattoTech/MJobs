"""Admission to real document-worker execution, using only synthetic applicant data."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.manual_capture import capture_manual_job
from tests.integration.test_spine import PROFILE
from workers.operations import claim, recover_expired, request_cancel
from workers.tasks import execute_operation


class PreparationPriorityTests(unittest.TestCase):
    def test_distinct_score_requests_cannot_overlap_for_one_profile(self):
        from threading import Barrier
        gate = Barrier(2)
        def refresh(request_id):
            gate.wait(timeout=5)
            return self.client.post("/matches", json={"profile_id": "fixture", "request_id": request_id})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(refresh, "first-tab")
            second = pool.submit(refresh, "second-tab")
            responses = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_concurrent_profile_scoring_and_preparation_admit_only_one_kind(self):
        from threading import Barrier
        job_id = self.job("scoring-race")
        for attempt in range(3):
            gate = Barrier(2)
            def post(path, body):
                gate.wait(timeout=5)
                return self.client.post(path, json=body)
            with ThreadPoolExecutor(max_workers=2) as pool:
                scoring = pool.submit(post, "/matches", {"profile_id": "fixture", "request_id": f"score-{attempt}"})
                preparation = pool.submit(post, "/applications", {"profile_id": "fixture", "job_id": job_id,
                                                                  "request_id": f"prepare-{attempt}"})
                responses = [scoring.result(timeout=15), preparation.result(timeout=15)]
            self.assertEqual(sorted(response.status_code for response in responses), [202, 409],
                             [response.text for response in responses])
            accepted = next(response.json()["operation_id"] for response in responses if response.status_code == 202)
            with self.store.engine.begin() as connection:
                active = connection.execute(select(m.operations.c.id).where(
                    m.operations.c.entity_id == "fixture", m.operations.c.state.in_(["QUEUED", "RUNNING"]))).scalars().all()
                self.assertEqual(active, [accepted])
                connection.execute(update(m.operations).where(m.operations.c.id == accepted).values(state="CANCELLED"))

    def database_url(self):
        return f"sqlite:///{(self.root / 'test.db').as_posix()}"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-priority-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.database_url())
        if self.store.url.startswith("sqlite:"):
            self.store.migrate()
        else:
            # Only the guarded, dedicated test database below can reach this.
            from sqlalchemy import text
            with self.store.engine.begin() as connection:
                connection.execute(text('TRUNCATE ' + ', '.join(
                    '"' + table.name + '"' for table in m.metadata.sorted_tables) + ' CASCADE'))
        self.addCleanup(self.store.engine.dispose)
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)
        self.env = patch.dict(os.environ, MJOBS_DATABASE_URL=self.store.url,
                              MJOBS_OUTPUT_DIR=str(self.root), MJOBS_OPERATION_MODE="FIXTURE",
                              MJOBS_DOCUMENT_GENERATION_CONCURRENCY="1")
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            operation_mode="FIXTURE"))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def job(self, label, *, age=1, skills=None):
        job = capture_manual_job(self.store, {
            "request_id": label, "title": f"Synthetic {label}", "company": "Example Systems",
            "description": "Responsibilities:\n- Build Python SQL services for this fictional employer.", "profile_id": "fixture",
            "application_url": f"https://boards.greenhouse.io/example/jobs/{label}",
            "reference_url": f"https://example.test/jobs/{label}",
        })["job"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(
                skills=skills if skills is not None else ["Python", "SQL"],
                attributes={**job["attributes"], "reported_posted_at":
                            (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()}))
        return job["id"]

    def admit(self, job_id, request_id, profile="fixture"):
        response = self.client.post("/applications", json={
            "profile_id": profile, "job_id": job_id, "request_id": request_id})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["operation_id"]

    def row(self, operation_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())

    def test_late_fresh_match_overtakes_older_delivery_and_both_generate_documents(self):
        low = self.admit(self.job("stale", age=1000, skills=["Java"]), "low")
        high = self.admit(self.job("fresh"), "high")
        # Deliver the old transport message first, just as an existing Redis FIFO would.
        execute_operation(low)
        self.assertEqual(self.row(low)["state"], "QUEUED")
        self.assertEqual(self.row(low)["attempt_count"], 0)
        self.assertEqual(execute_operation(high)["status"], "COMPLETED")
        self.assertEqual(execute_operation(low)["status"], "COMPLETED")
        self.assertLess(self.row(high)["started_at"], self.row(low)["started_at"])
        self.assertEqual(self.store.count("applications"), 2)
        self.assertEqual(self.store.count("documents"), 4)
        self.assertTrue(all(a["state"] == "PREPARED" for a in self.store.applications()))

    def test_admission_snapshot_replay_reopen_and_scoped_readback(self):
        job = self.job("snapshot")
        operation = self.admit(job, "snapshot")
        snapshot = self.row(operation)["params"]["preparation_priority"]
        self.assertEqual(snapshot["tier"], 0)
        self.assertEqual(snapshot["match_score"], 100)
        self.assertEqual(self.admit(job, "snapshot"), operation)
        reopened = Store(self.store.url)
        try:
            with reopened.engine.connect() as connection:
                row = connection.execute(select(m.operations).where(m.operations.c.id == operation)).mappings().one()
            self.assertEqual(row["params"]["preparation_priority"], snapshot)
        finally:
            reopened.engine.dispose()
        public = self.client.get("/profiles/fixture/operations/progress").json()["operations"][0]
        self.assertEqual(public["preparation_priority"]["tier"], 0)
        self.assertNotIn("profile_version", public["preparation_priority"])
        self.assertNotIn("job_fingerprint", public["preparation_priority"])

    def test_aging_and_stable_ties_do_not_reset_on_redispatch(self):
        old = self.admit(self.job("old", age=1000, skills=["Java"]), "old")
        young = self.admit(self.job("young"), "young")
        row = self.row(old)
        metadata = dict(row["params"]["preparation_priority"])
        metadata["enqueued_at"] = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == old).values(
                params={**row["params"], "preparation_priority": metadata}))
        execute_operation(young)
        self.assertEqual(self.row(young)["attempt_count"], 0)
        self.assertEqual(execute_operation(old)["status"], "COMPLETED")
        self.assertEqual(execute_operation(young)["status"], "COMPLETED")
        self.assertEqual(self.row(old)["params"]["preparation_priority"], metadata)

    def test_cancelled_high_priority_does_not_block_and_duplicate_never_reopens(self):
        low = self.admit(self.job("low", age=1000), "low")
        job = self.job("cancel")
        high = self.admit(job, "cancel")
        request_cancel(self.store, high)
        self.assertEqual(execute_operation(low)["status"], "COMPLETED")
        self.assertEqual(self.admit(job, "cancel"), high)
        self.assertEqual(self.row(high)["state"], "CANCELLED")
        self.assertEqual(self.row(high)["attempt_count"], 0)

    def test_job_change_after_admission_blocks_document_generation(self):
        job = self.job("changed")
        operation = self.admit(job, "changed")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job).values(
                description="Responsibilities:\n- Maintain different Python SQL services."))
        execute_operation(operation)
        self.assertNotEqual(self.row(operation)["state"], "COMPLETED")
        self.assertEqual(self.store.count("documents"), 0)

    def test_profile_isolation_and_conflicting_request_identity(self):
        other = copy.deepcopy(PROFILE)
        other.update(id="other", facts=[{**PROFILE["facts"][0], "text": "Java"}])
        self.store.save_profile(other)
        job = self.job("scope")
        mine = self.admit(job, "shared", "fixture")
        theirs = self.admit(job, "shared", "other")
        self.assertNotEqual(mine, theirs)
        self.assertEqual(self.row(mine)["params"]["preparation_priority"]["match_score"], 100)
        self.assertEqual(self.row(theirs)["params"]["preparation_priority"]["match_score"], 0)
        rows = self.client.get("/profiles/other/operations/progress").json()["operations"]
        self.assertEqual([r["operation_id"] for r in rows], [theirs])
        changed = self.client.post("/applications", json={"profile_id": "fixture",
            "job_id": self.job("different"), "request_id": "shared"})
        self.assertEqual(changed.status_code, 409)

    def test_equal_tier_uses_enqueue_time_then_id_even_when_delivery_is_reversed(self):
        first = self.admit(self.job("equal-one"), "one")
        second = self.admit(self.job("equal-two"), "two")
        execute_operation(second)
        self.assertEqual(self.row(second)["attempt_count"], 0)
        timestamp = self.row(first)["params"]["preparation_priority"]["enqueued_at"]
        row = self.row(second)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == second).values(
                params={**row["params"], "preparation_priority": {
                    **row["params"]["preparation_priority"], "enqueued_at": timestamp}}))
        winner, later = sorted([first, second])
        execute_operation(later)
        self.assertEqual(self.row(later)["attempt_count"], 0)
        self.assertEqual(execute_operation(winner)["status"], "COMPLETED")
        self.assertEqual(execute_operation(later)["status"], "COMPLETED")

    def test_profile_change_after_admission_blocks_before_documents(self):
        operation = self.admit(self.job("profile-change"), "change")
        self.store.save_profile({**PROFILE, "name": "Changed synthetic applicant"})
        execute_operation(operation)
        self.assertNotEqual(self.row(operation)["state"], "COMPLETED")
        self.assertEqual(self.store.count("documents"), 0)

    def test_retry_due_and_expired_lease_preserve_priority_without_duplicate_output(self):
        low = self.admit(self.job("retry-low", age=1000), "low")
        high = self.admit(self.job("retry-high"), "high")
        snapshot = self.row(high)["params"]["preparation_priority"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == high).values(
                next_attempt_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
        self.assertEqual(execute_operation(low)["status"], "COMPLETED")
        self.assertIsNone(claim(self.store, high))
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == high).values(next_attempt_at=None))
        self.assertIsNotNone(claim(self.store, high))
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == high).values(
                lease_expires_at="2000-01-01T00:00:00+00:00"))
        recover_expired(self.store)
        self.assertEqual(self.row(high)["params"]["preparation_priority"], snapshot)
        self.assertEqual(execute_operation(high)["status"], "COMPLETED")
        execute_operation(high)
        self.assertEqual(self.row(high)["attempt_count"], 2)
        self.assertEqual(self.store.count("documents"), 4)

    def test_unknown_evidence_is_background_and_priority_cannot_be_supplied_by_client(self):
        job = self.job("unknown", skills=[])
        operation = self.admit(job, "unknown")
        snapshot = self.row(operation)["params"]["preparation_priority"]
        self.assertIsNone(snapshot["match_score"])
        self.assertEqual(snapshot["queue_priority"], "BACKGROUND")
        response = self.client.post("/applications", json={"profile_id": "fixture", "job_id": job,
            "request_id": "injected", "preparation_priority": {"tier": 0}})
        self.assertEqual(response.status_code, 422)

    def test_concurrent_admission_and_claims_across_independent_store_engines(self):
        job = self.job("concurrent")
        def admit():
            store = Store(self.store.url)
            try:
                with TestClient(create_app(store, "http://127.0.0.1:8090", self.root,
                                           operation_mode="FIXTURE")) as client:
                    response = client.post("/applications", json={"profile_id": "fixture",
                        "job_id": job, "request_id": "concurrent"})
                    self.assertEqual(response.status_code, 202, response.text)
                    return response.json()["operation_id"]
            finally:
                store.engine.dispose()
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(lambda _: admit(), range(2)))
        self.assertEqual(first, second)
        self.assertEqual(self.store.count("worker_operations"), 1)
        low = self.admit(self.job("concurrent-low", age=1000), "low")
        def compete(operation):
            store = Store(self.store.url)
            try:
                return claim(store, operation)
            finally:
                store.engine.dispose()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, [low, first]))
        self.assertIsNone(results[0])
        self.assertEqual(results[1]["id"], first)
        self.assertEqual(self.row(low)["attempt_count"], 0)

    def test_changed_input_during_admission_does_not_create_a_queue_row(self):
        job = self.job("admission-race")
        from services.matching.preparation_priority import admission_snapshot
        def changed(job_value, profile, timestamp):
            snapshot = admission_snapshot(job_value, profile, timestamp)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == job).values(
                    title="Changed after approval"))
            return snapshot
        with patch("services.matching.preparation_priority.admission_snapshot", side_effect=changed):
            response = self.client.post("/applications", json={"profile_id": "fixture", "job_id": job,
                "request_id": "changed"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.count("worker_operations"), 0)


@unittest.skipUnless(os.environ.get("MJOBS_PRIORITY_POSTGRES_URL"), "Requires isolated priority PostgreSQL")
class PostgreSQLPreparationPriorityTests(PreparationPriorityTests):
    @classmethod
    def setUpClass(cls):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy.engine import make_url
        url = os.environ["MJOBS_PRIORITY_POSTGRES_URL"]
        target = make_url(url)
        if target.database != "mjobs_priority_validation" or target.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Requires disposable loopback database mjobs_priority_validation")
        with patch.dict(os.environ, MJOBS_DATABASE_URL=url):
            command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")

    def database_url(self):
        return os.environ["MJOBS_PRIORITY_POSTGRES_URL"]

    @unittest.skipUnless(os.environ.get("MJOBS_PRIORITY_REDIS_URL"), "Requires isolated priority Redis")
    @unittest.skipIf(os.name == "nt", "Production forking worker runs in Linux")
    def test_cold_worker_start_does_not_inherit_dispatcher_import_lock(self):
        import signal
        import subprocess
        import sys
        import time
        from redis import Redis
        from workers.dispatch import dispatch_pending
        from urllib.parse import urlsplit

        redis_url = os.environ["MJOBS_PRIORITY_REDIS_URL"]
        target = urlsplit(redis_url)
        if target.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"} or target.path != "/15":
            raise ValueError("Requires disposable local Redis database 15")
        operation = self.admit(self.job("cold-start"), "cold-start")
        self.assertEqual(dispatch_pending(self.store, redis_url)["count"], 1)
        # A slow first import on the dispatcher thread deterministically exposes
        # the fork race seen after Redis/Compose restart. Use the production
        # entry point, a fresh interpreter, and a real RQ work horse.
        program = '''
import importlib.machinery, sys, threading, time
class SlowHTTPImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "httpx":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        original = spec.loader.exec_module
        def execute(module):
            if threading.current_thread() is not threading.main_thread():
                time.sleep(3)
            original(module)
        spec.loader.exec_module = execute
        return spec
sys.meta_path.insert(0, SlowHTTPImport())
from workers.main import main
main()
'''
        environment = {**os.environ, "MJOBS_REDIS_URL": redis_url,
                       "MJOBS_WORKER_QUEUE": "mjobs-document"}
        with (self.root / "cold-worker.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-c", program], env=environment,
                                       stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and self.row(operation)["state"] == "QUEUED":
                    time.sleep(.1)
                while time.monotonic() < deadline and self.row(operation)["state"] == "RUNNING":
                    time.sleep(.1)
                self.assertEqual(self.row(operation)["state"], "COMPLETED")
                self.assertEqual(self.row(operation)["attempt_count"], 1)
                self.assertEqual(self.store.count("documents"), 2)
            finally:
                # Reap only this test's process group, including its RQ child.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                redis = Redis.from_url(redis_url)
                try:
                    from rq.job import Job
                    Job.fetch(operation, connection=redis).delete()
                finally:
                    redis.close()

    @unittest.skipUnless(os.environ.get("MJOBS_PRIORITY_REDIS_URL"), "Requires isolated priority Redis")
    @unittest.skipIf(os.name == "nt", "Production RQ Worker acceptance runs in the isolated Linux runtime")
    def test_real_redis_fifo_worker_processes_choose_priority_and_rebuild_lost_queue(self):
        from redis import Redis
        from rq import Queue, SpawnWorker, Worker
        from workers.dispatch import dispatch_pending

        redis_url = os.environ["MJOBS_PRIORITY_REDIS_URL"]
        from urllib.parse import urlsplit
        target = urlsplit(redis_url)
        if target.hostname not in {"127.0.0.1", "localhost", "host.docker.internal"} or target.path != "/15":
            raise ValueError("Requires disposable local Redis database 15")
        redis = Redis.from_url(redis_url)
        self.addCleanup(redis.close)
        queue = Queue("mjobs-document", connection=redis)
        self.assertEqual(queue.count, 0, "Disposable Redis queue must start empty")
        low = self.admit(self.job("redis-stale", age=1000, skills=["Java"]), "low")
        self.assertEqual(dispatch_pending(self.store, redis_url)["count"], 1)
        high = self.admit(self.job("redis-fresh"), "high")
        self.assertEqual(dispatch_pending(self.store, redis_url)["count"], 1)
        self.assertEqual(queue.job_ids, [low, high])
        worker_type = SpawnWorker if os.name == "nt" else Worker
        worker_type([queue], connection=redis, job_monitoring_interval=1).work(burst=True, logging_level="ERROR")
        self.assertEqual(self.row(high)["state"], "COMPLETED")
        self.assertEqual(self.row(low)["state"], "QUEUED")
        self.assertEqual(self.row(low)["attempt_count"], 0)
        # Transport loss is recoverable from PostgreSQL; no application data lives in Redis.
        self.assertEqual(dispatch_pending(self.store, redis_url)["count"], 1)
        queue.remove(low)
        from rq.job import Job
        Job.fetch(low, connection=redis).delete()
        self.store.engine.dispose()
        self.assertEqual(dispatch_pending(self.store, redis_url)["count"], 1)
        worker_type([queue], connection=redis, job_monitoring_interval=1).work(burst=True, logging_level="ERROR")
        self.assertEqual(self.row(low)["state"], "COMPLETED")
        self.assertLess(self.row(high)["started_at"], self.row(low)["started_at"])
        self.assertEqual(self.row(low)["attempt_count"], 1)
        self.assertEqual(self.store.count("documents"), 4)
        self.assertEqual(dispatch_pending(self.store, redis_url)["count"], 0)


if __name__ == "__main__":
    unittest.main()
