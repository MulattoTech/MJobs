"""Jobs API keeps discovery origin separate from application-route provider."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class JobRouteProvenanceApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-route-provenance-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = next(
            job["id"] for job in self.store.jobs("fixture")
            if any(posting["source"] == "mock-board"
                   for posting in self.store.job(job["id"])["postings"])
        )
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_discovery_origin_route_provider_and_ats_evidence_remain_independent(self):
        # The fixture aggregator supplied this posting; the employer application route
        # is separately persisted as mock-ats.
        persisted = self.store.job(self.job_id)
        self.assertIn("mock-board", [posting["source"] for posting in persisted["postings"]])
        self.assertEqual(persisted["provider"], "mock-ats")

        detail_response = self.client.get(f"/jobs/{self.job_id}")
        self.assertEqual(detail_response.status_code, 200, detail_response.text)
        detail = detail_response.json()
        self.assertIn("mock-board", [posting["source"] for posting in detail["postings"]])
        self.assertEqual(detail["provider"], "mock-ats")

        route_filtered = self.client.get("/jobs", params={"provider": "mock-ats"})
        self.assertEqual(route_filtered.status_code, 200, route_filtered.text)
        self.assertIn(self.job_id, [row["id"] for row in route_filtered.json()["jobs"]])
        ats_filtered_with_evidence = self.client.get(
            "/jobs", params={"ats_provider": "mock-ats"})
        self.assertIn(self.job_id, [row["id"] for row in
                                    ats_filtered_with_evidence.json()["jobs"]])

        # A route provider is not itself ATS verification: without qualifying
        # independent persisted evidence, the ATS-specific facet must exclude it.
        with self.store.engine.begin() as connection:
            route = connection.execute(select(m.routes).where(
                m.routes.c.job_id == self.job_id,
                m.routes.c.provider == "mock-ats",
            )).mappings().one()
            connection.execute(update(m.company_ats_tenants).where(
                m.company_ats_tenants.c.company_id == route["company_id"],
                m.company_ats_tenants.c.provider == route["provider"],
                m.company_ats_tenants.c.tenant_identifier == route["tenant"],
            ).values(evidence=[]))

        route_filtered_without_ats_evidence = self.client.get(
            "/jobs", params={"provider": "mock-ats"})
        self.assertIn(self.job_id, [row["id"] for row in
                                    route_filtered_without_ats_evidence.json()["jobs"]])
        ats_filtered_without_evidence = self.client.get(
            "/jobs", params={"ats_provider": "mock-ats"})
        self.assertEqual(ats_filtered_without_evidence.status_code, 200,
                         ats_filtered_without_evidence.text)
        self.assertNotIn(self.job_id, [row["id"] for row in
                                      ats_filtered_without_evidence.json()["jobs"]])
