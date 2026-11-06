"""Aggregator child request -> real Redis/RQ company worker -> durable result."""
from copy import deepcopy
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from redis import Redis
from rq import Queue, Worker
from rq.job import Job
from rq.registry import DeferredJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.aggregator_bootstrap import BudgetedPublicClient
from services.company_discovery.public_client import PublicCompanyClient
from services.company_discovery.requests import list_discovery_requests
from services.discovery.sources.greenhouse import Greenhouse
from tests.integration.test_official_domain_canonical_apply_proof import _apply_page, _job_page
from workers.dispatch import dispatch_pending


COMPANY_QUEUE = "mjobs-company-discovery"
WRONG_QUEUE = "mjobs-discovery"


def _admit_redis_url(value):
    target = urlsplit(value)
    if (target.scheme != "redis" or target.hostname != "127.0.0.1"
            or target.port != 6379 or target.path != "/0" or target.username
            or target.password or target.query or target.fragment):
        raise ValueError("Requires disposable loopback Redis on port 6379, database 0")
    return value


@unittest.skipUnless(sys.platform == "linux", "NOT_RUN: production RQ work horse requires Linux")
@unittest.skipUnless(os.environ.get("MJOBS_REDIS_URL"), "NOT_RUN: disposable CI Redis URL absent")
class AggregatorBootstrapRealRQTests(unittest.TestCase):
    def setUp(self):
        self.redis_url = _admit_redis_url(os.environ["MJOBS_REDIS_URL"])
        self.redis = Redis.from_url(self.redis_url, socket_connect_timeout=2, socket_timeout=2)
        self.addCleanup(self.redis.close)
        self.redis.ping()
        for name in (COMPANY_QUEUE, WRONG_QUEUE):
            self.assertEqual(Queue(name, connection=self.redis).get_job_ids(), [],
                             "Refusing preexisting queue work")
            for registry in (StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry):
                self.assertEqual(registry(name, connection=self.redis).get_job_ids(cleanup=False), [],
                                 "Refusing preexisting active or scheduled work")
        self.assertFalse([worker for worker in Worker.all(connection=self.redis)
                          if set(worker.queue_names()) & {COMPANY_QUEUE, WRONG_QUEUE}],
                         "Refusing a queue already owned by another worker")

        from tests.integration.test_aggregator_bootstrap_collection import AggregatorBootstrapCollectionTests

        self.case = AggregatorBootstrapCollectionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)
        self.output = tempfile.TemporaryDirectory(prefix="mjobs-aggregator-rq-output-")
        self.addCleanup(self.output.cleanup)
        self.owned_jobs = set()
        self.addCleanup(self._delete_owned_jobs)
        raw = self.case.rows[0]
        self.title = raw["title"]
        self.company = raw["company_name"]
        raw["description"] += (
            '<p><a href="https://job-boards.greenhouse.io/acme/jobs/1001">Apply to this job</a></p>'
        )
        self.job_url = "https://acme.example/careers/listing/senior-python-engineer/1001"
        self.apply_url = "https://acme.example/careers/apply/senior-python-engineer/1001"
        self.job_html = _job_page(
            canonical=self.job_url, job_url=self.job_url, job_id="1001",
            title=self.title, company=self.company, apply_href=self.apply_url,
        ).replace("https://employer.example", "https://acme.example")
        self.apply_html = _apply_page(iframe_src=(
            "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1001"))
        self.native_job = {
            "id": 1001, "title": self.title, "absolute_url": self.job_url,
            "content": "<p>Build Python SQL backend services.</p>",
        }

    def _delete_owned_jobs(self):
        for identifier in self.owned_jobs:
            if Job.exists(identifier, connection=self.redis):
                Job.fetch(identifier, connection=self.redis).delete()

    def _controlled_public_get(self, url, _deadline):
        """Existing PublicCompanyClient test seam; every response is synthetic."""
        request = httpx.Request("GET", url)
        parsed = request.url
        if parsed.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if (parsed.host == "boards-api.greenhouse.io"
                and parsed.path == "/v1/boards/acme/jobs/1001"):
            return httpx.Response(200, json=self.native_job, request=request)
        if url == self.job_url:
            return httpx.Response(200, text=self.job_html, request=request)
        if url == self.apply_url:
            return httpx.Response(200, text=self.apply_html, request=request)
        return httpx.Response(404, request=request)

    def _greenhouse_board(self, suffix, params=None):
        if not suffix:
            return {"name": self.company}
        return {"jobs": [deepcopy(self.native_job)]}

    def test_aggregator_child_is_dispatched_and_completed_by_real_rq_worker(self):
        # Build the same real parent/child persistence graph used by collection:
        # a running DISCOVER parent stages an aggregator hint and enqueues one
        # COMPANY_DISCOVER child. The collector's source HTTP is MockTransport.
        lineage = self.case.parent_operation()
        self.case.collect(**lineage)
        with self.case.store.engine.connect() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests)).mappings().one())
            child = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == request["operation_id"])).mappings().one())
        self.assertEqual(request["authorization"], "AGGREGATOR_BOOTSTRAP_PENDING")
        self.assertEqual(child["kind"], "COMPANY_DISCOVER")
        self.assertEqual(child["params"]["aggregator_bootstrap"], "AGGREGATOR_BOOTSTRAP_PENDING_V1")
        parent_binding = request["domain_evidence"][0]["binding"]
        self.assertEqual(parent_binding["parent_operation_id"], lineage["parent_operation_id"])
        self.assertEqual(parent_binding["parent_operation_attempt"], lineage["parent_operation_attempt"])
        self.owned_jobs.add(child["id"])

        # The parent has finished collection; child authorization must remain
        # bound to that exact completed parent attempt.
        with self.case.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == lineage["parent_operation_id"]).values(state="COMPLETED"))
        reopened = Store(self.case.url)
        try:
            with reopened.engine.connect() as connection:
                saved_parent = connection.execute(select(m.operations).where(
                    m.operations.c.id == lineage["parent_operation_id"])).mappings().one()
                saved_request = connection.execute(select(m.company_discovery_requests).where(
                    m.company_discovery_requests.c.id == request["id"])).mappings().one()
            self.assertEqual(saved_parent["attempt_count"], lineage["parent_operation_attempt"])
            self.assertEqual(saved_request["operation_id"], child["id"])
        finally:
            reopened.engine.dispose()

        self.assertEqual(dispatch_pending(self.case.store, self.redis_url),
                         {"status": "DISPATCHED", "count": 1})
        job = Job.fetch(child["id"], connection=self.redis)
        self.assertEqual(job.origin, COMPANY_QUEUE)
        self.assertEqual(job.func_name, "workers.tasks.execute_operation")
        self.assertEqual(job.args, (child["id"],))
        self.assertEqual(Queue(COMPANY_QUEUE, connection=self.redis).get_job_ids(), [child["id"]])

        # The production RQ Worker consumes the real Redis job and forks its
        # normal work horse. Only the established public-client test seam and
        # Greenhouse collector are controlled; no socket or public endpoint is
        # contacted, and application submission is not part of this workflow.
        worker = Worker([COMPANY_QUEUE], connection=self.redis)
        environment = {
            "MJOBS_DATABASE_URL": self.case.url,
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_REDIS_URL": self.redis_url,
            "MJOBS_OUTPUT_DIR": self.output.name,
            "MJOBS_OPERATION_TIMEOUT": "30",
        }
        from services.company_discovery import aggregator_workflow

        real_resolver = aggregator_workflow.corroborate_aggregator_job

        def controlled_resolver(*args, **kwargs):
            return real_resolver(*args, client_factory=BudgetedPublicClient, **kwargs)

        with (patch.dict(os.environ, environment),
              patch.object(PublicCompanyClient, "_request_once",
                           side_effect=self._controlled_public_get),
              patch.object(Greenhouse, "_get", side_effect=self._greenhouse_board),
              patch.object(aggregator_workflow, "corroborate_aggregator_job",
                           side_effect=controlled_resolver)):
            self.assertTrue(worker.work(burst=True, logging_level="WARNING"))

        self.assertEqual(job.get_status(refresh=True), "finished")
        self.assertEqual(job.return_value()["status"], "COMPLETED")
        self.assertEqual(dispatch_pending(self.case.store, self.redis_url)["count"], 0)
        reopened = Store(self.case.url)
        try:
            with reopened.engine.connect() as connection:
                operation = dict(connection.execute(select(m.operations).where(
                    m.operations.c.id == child["id"])).mappings().one())
                saved_request = dict(connection.execute(select(m.company_discovery_requests).where(
                    m.company_discovery_requests.c.id == request["id"])).mappings().one())
                sources = list(connection.execute(select(m.company_sources).where(
                    m.company_sources.c.company_id == request["company_id"])).mappings())
            self.assertEqual((operation["state"], operation["attempt_count"]), ("COMPLETED", 1))
            self.assertEqual(operation["progress"], 100)
            public_request = next(row for row in list_discovery_requests(reopened)
                                  if row["id"] == request["id"])
            self.assertEqual(public_request["status"], "COMPLETED")
            self.assertEqual(saved_request["operation_id"], child["id"])
            self.assertEqual(saved_request["authorization"], "AGGREGATOR_BOOTSTRAP_VERIFIED")
            receipt = saved_request["domain_evidence"][1]["receipt"]
            self.assertEqual(receipt["execution_scope"], "CONTROLLED_TRANSPORT")
            self.assertGreaterEqual(receipt["budget"]["http_requests"], 3)
            self.assertEqual(receipt["native"]["tenant"], "acme")
            self.assertTrue(any(source["provider"] == "greenhouse" and source["enabled"]
                                for source in sources))
            self.assertEqual(Queue(COMPANY_QUEUE, connection=self.redis).get_job_ids(), [])
            self.assertEqual(reopened.count("applications"), 0)
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
