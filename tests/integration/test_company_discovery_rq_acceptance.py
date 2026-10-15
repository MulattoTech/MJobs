"""MJ-REQ-263: real Linux RQ transport, plus offline nonblocking import proof.

The transport case requires the existing disposable CI Redis service. SQLite is
private to this test and shared with the actual forking worker, not PG evidence.
No direct task execution, job.perform(), queue mocks, or Redis-wide cleanup.
"""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from redis import Redis
from rq import Queue, Worker
from rq.job import Job
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import select

from fixtures.mock_server import create_mock_app, fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.requests import list_discovery_requests
from services.company_discovery.triggers import queue_company_discovery
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE
from workers.dispatch import dispatch_pending


ROOT = Path(__file__).resolve().parents[2]
COMPANY_QUEUE = "mjobs-company-discovery"
WRONG_QUEUE = "mjobs-discovery"


def _child_environment():
    # No inherited user databases, provider credentials, proxies or test URLs.
    return {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR",
                                            "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if key in os.environ}


def _admit_redis_url(url):
    """Reject arbitrary targets before any connection or worker is created."""
    target = urlsplit(url)
    if (target.scheme != "redis" or target.hostname not in {"127.0.0.1", "localhost"}
            or target.port != 6379 or target.path != "/0" or target.username
            or target.password or target.query or target.fragment):
        raise ValueError("Requires disposable loopback Redis on port 6379, database 0")
    return url


@contextmanager
def _private_store(root, mode):
    store = Store(f"sqlite:///{(root / 'acceptance.db').as_posix()}")
    try:
        store.migrate()
        store.bind_workspace_mode(mode)
        store.save_profile(deepcopy(PROFILE))
        yield store
    finally:
        store.engine.dispose()


def _import_without_employer_egress(test, root):
    """Two actual imports finish with review pending and no company consumer.

    Public-source payloads are synthetic. Employer HTTP must never execute on
    the importer, even if an exception from it would otherwise be swallowed.
    Unverified origins stay NEEDS_ORIGIN_REVIEW; do not bypass PERSONAL SSRF.
    """
    root.mkdir()
    with _private_store(root, "PERSONAL") as store:
        with patch.object(PublicCompanyClient, "get", side_effect=AssertionError(
                "Employer discovery ran synchronously during import")) as egress:
            for index, tenant in enumerate(("example", "example-two"), start=1):
                source = configure_source(store, tenant)
                raw = {**RAW, "absolute_url": f"https://job-boards.greenhouse.io/{tenant}/jobs/1234"}

                def response(request):
                    if request.url.path.endswith("/jobs"):
                        return httpx.Response(200, json={"jobs": [raw], "meta": {"total": 1}})
                    return httpx.Response(200, json={"name": "Synthetic Example Employer"})

                result = collect_source(store, source, "fixture",
                                        transport=httpx.MockTransport(response))
                test.assertEqual(result["jobs_seen"], 1)
                test.assertEqual(store.count("canonical_jobs"), index)
                requests = list_discovery_requests(store)
                test.assertEqual(len(requests), index)
                test.assertTrue(all(row["status"] == "NEEDS_ORIGIN_REVIEW" for row in requests))
                test.assertTrue(all(row["operation_id"] is None for row in requests))
                test.assertEqual(store.count("worker_operations"), 0)
            egress.assert_not_called()
        reopened = Store(store.url)
        try:
            test.assertEqual(reopened.count("canonical_jobs"), 2)
            test.assertEqual([row["status"] for row in list_discovery_requests(reopened)],
                             ["NEEDS_ORIGIN_REVIEW", "NEEDS_ORIGIN_REVIEW"])
        finally:
            reopened.engine.dispose()


def _bounded_import_probe(test, root):
    result = subprocess.run(
        [sys.executable, "-m", "tests.integration.test_company_discovery_rq_acceptance", str(root)],
        cwd=ROOT, env=_child_environment(), capture_output=True, text=True, timeout=20)
    test.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-6000:])
    test.assertEqual(result.stdout.strip(), "Two source imports persisted without employer egress")


class CompanyDiscoveryRQAdmissionTests(unittest.TestCase):
    def test_redis_target_admission_rejects_non_disposable_urls_before_io(self):
        self.assertEqual(_admit_redis_url("redis://127.0.0.1:6379/0"),
                         "redis://127.0.0.1:6379/0")
        for url in ("redis://remote.example:6379/0", "redis://127.0.0.1:6379/15",
                    "redis://127.0.0.1:6380/0", "redis://user:secret@127.0.0.1:6379/0",
                    "rediss://127.0.0.1:6379/0", "redis://127.0.0.1:6379/0?db=1"):
            with self.subTest(target=url.split("@")[-1]), self.assertRaises(ValueError):
                _admit_redis_url(url)

    def test_source_import_finishes_without_company_consumer_or_employer_egress(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-company-import-") as directory:
            _bounded_import_probe(self, Path(directory) / "import")


@unittest.skipUnless(sys.platform == "linux", "NOT_RUN: production RQ work horse requires Linux")
@unittest.skipUnless(os.environ.get("MJOBS_REDIS_URL"), "NOT_RUN: disposable CI Redis URL absent")
class CompanyDiscoveryRealRQTests(unittest.TestCase):
    def setUp(self):
        self.redis_url = _admit_redis_url(os.environ["MJOBS_REDIS_URL"])
        self.redis = Redis.from_url(self.redis_url, socket_connect_timeout=2, socket_timeout=2)
        self.addCleanup(self.redis.close)
        # A configured but unavailable service must FAIL, not silently skip.
        self.redis.ping()
        for name in (COMPANY_QUEUE, WRONG_QUEUE):
            self.assertEqual(Queue(name, connection=self.redis).get_job_ids(), [],
                             "Refusing to consume a queue with preexisting work")
            for registry in (StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry):
                self.assertEqual(registry(name, connection=self.redis).get_job_ids(cleanup=False), [],
                                 "Refusing a queue with preexisting active or scheduled work")
        self.assertFalse([worker for worker in Worker.all(connection=self.redis)
                          if set(worker.queue_names()) & {COMPANY_QUEUE, WRONG_QUEUE}],
                         "Refusing queues already owned by another worker")
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-company-rq-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.operations = set()
        self.addCleanup(self._delete_owned_jobs)

    def _delete_owned_jobs(self):
        for identifier in self.operations:
            if Job.exists(identifier, connection=self.redis):
                Job.fetch(identifier, connection=self.redis).delete()

    @contextmanager
    def _worker(self, store, origin, *, company=True):
        # Strip inherited provider, user database, proxy and test configuration.
        environment = _child_environment()
        environment.update(MJOBS_DATABASE_URL=store.url, MJOBS_REDIS_URL=self.redis_url,
                           MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root / "outputs"),
                           MJOBS_OPERATION_MODE="FIXTURE", MJOBS_WORKER_QUEUE=WRONG_QUEUE,
                           MJOBS_OPERATION_TIMEOUT="30", PYTHONUNBUFFERED="1")
        module = "workers.company_discovery_worker" if company else "workers.main"
        with (self.root / ("company-worker.log" if company else "wrong-worker.log")).open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, env=environment,
                                       stdout=log, stderr=log, start_new_session=True)
            owned_workers = {}
            try:
                def registered():
                    self.assertIsNone(process.poll(), "Fresh worker exited before registration")
                    for worker in Worker.all(connection=self.redis):
                        if worker.pid == process.pid:
                            owned_workers[worker.name] = worker
                            expected = COMPANY_QUEUE if company else WRONG_QUEUE
                            self.assertEqual(worker.queue_names(), [expected])
                            return True
                    return False

                self._until(registered, "Fresh production worker never registered its queue")
                yield process
            finally:
                # Kill only this new session/process group, including an RQ work horse
                # even if its parent exits first. Never target another worker PID.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                for worker in Worker.all(connection=self.redis):
                    if worker.pid == process.pid:
                        owned_workers[worker.name] = worker
                for worker in owned_workers.values():
                    worker.register_death()

    def _until(self, predicate, message):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail(message)

    def test_real_dispatch_wrong_queue_import_outage_and_fresh_company_worker(self):
        requests = []
        app = create_mock_app()

        @app.middleware("http")
        async def observe(request, call_next):
            requests.append(request.url.path)
            return await call_next(request)

        with patch("fixtures.mock_server.create_mock_app", return_value=app), fixture_server() as origin:
            database = self.root / "worker"
            database.mkdir()
            with _private_store(database, "FIXTURE") as store:
                candidate = {"company_name": "Example Systems Inc.",
                             "company_website": origin + "/company",
                             "application_url": origin + "/board/apply/A"}
                queued = queue_company_discovery(store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture")
                identifier = queued["operation_id"]
                self.operations.add(identifier)
                replay = queue_company_discovery(store, candidate, "PREVIOUSLY_UNSEEN_COMPANY", "fixture")
                self.assertEqual(replay["operation_id"], identifier)
                self.assertEqual(store.count("worker_operations"), 1)
                self.assertEqual(dispatch_pending(store, self.redis_url), {"status": "DISPATCHED", "count": 1})
                self.assertEqual(dispatch_pending(store, self.redis_url)["count"], 0)
                job = Job.fetch(identifier, connection=self.redis)
                self.assertEqual(job.origin, COMPANY_QUEUE)
                self.assertEqual(job.func_name, "workers.tasks.execute_operation")
                self.assertEqual(job.args, (identifier,))
                self.assertEqual(Queue(COMPANY_QUEUE, connection=self.redis).get_job_ids(), [identifier])

                # The real source consumer can dispatch outbox rows but cannot
                # consume this company's queue. No company consumer exists yet.
                with self._worker(store, origin, company=False):
                    _bounded_import_probe(self, self.root / "import")
                    self.assertEqual(job.get_status(refresh=True), "queued")
                    self.assertEqual(requests, [])
                    with store.engine.connect() as connection:
                        row = connection.execute(select(m.operations).where(
                            m.operations.c.id == identifier)).mappings().one()
                    self.assertEqual((row["state"], row["attempt_count"]), ("QUEUED", 0))

                with self._worker(store, origin) as process:
                    def finished():
                        self.assertIsNone(process.poll(), "Company consumer exited before completion")
                        with store.engine.connect() as connection:
                            state = connection.execute(select(m.operations.c.state).where(
                                m.operations.c.id == identifier)).scalar_one()
                        self.assertNotIn(state, {"FAILED", "BLOCKED", "DEAD_LETTERED"})
                        return state == "COMPLETED" and job.get_status(refresh=True) == "finished"

                    self._until(finished, "Company RQ delivery did not finish within the bounded wait")
                    self.assertEqual(dispatch_pending(store, self.redis_url)["count"], 0)

                reopened = Store(store.url)
                try:
                    with reopened.engine.connect() as connection:
                        operation = connection.execute(select(m.operations).where(
                            m.operations.c.id == identifier)).mappings().one()
                        source = connection.execute(select(m.company_sources)).mappings().one()
                    self.assertEqual((operation["state"], operation["attempt_count"]), ("COMPLETED", 1))
                    self.assertTrue(source["discovered_automatically"])
                    self.assertEqual(reopened.count("company_job_sources"), 1)
                    self.assertEqual({job["requisition"] for job in reopened.jobs("fixture")}, {"A", "B"})
                finally:
                    reopened.engine.dispose()
                self.assertIn("/company", requests)
                self.assertIn("/ats/example/jobs", requests)
                self.assertEqual(httpx.get(origin + "/_test/request-counts", trust_env=False).json(),
                                 {"review": 0, "submit": 0})
                self.assertEqual(Queue(COMPANY_QUEUE, connection=self.redis).get_job_ids(), [])


if __name__ == "__main__":
    # Same-file child probe: no worker/task execution and no Redis connection.
    if len(sys.argv) != 2:
        raise SystemExit("Expected one test-private import directory")
    _import_without_employer_egress(unittest.TestCase(), Path(sys.argv[1]))
    print("Two source imports persisted without employer egress")
