"""Profile-scoped API integration for the persisted source relationship graph."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.source_relationship_graph import create_source_relationship_graph_router
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SourceRelationshipGraphApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-source-graph-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'graph.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        for profile_id, email in (("fixture", "alex@example.test"),
                                  ("other", "other@example.test"),
                                  ("empty", "empty@example.test")):
            self.store.save_profile({**deepcopy(PROFILE), "id": profile_id, "email": email})
        self._seed_graph()

        app = FastAPI()
        app.include_router(create_source_relationship_graph_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_graph(self):
        with self.store.engine.begin() as connection:
            for company_id, name in (("company-a", "Alpha Systems"), ("company-b", "Beta Systems")):
                connection.execute(m.companies.insert().values(
                    id=company_id, name=name, domain=f"{company_id}.example.test"))
            for company_id, job_id, title, profile_id in (
                    ("company-a", "job-a", "Platform Engineer", "fixture"),
                    ("company-b", "job-b", "Network Engineer", "other")):
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id=company_id, requisition=f"REQ-{job_id}",
                    title=title, description="Private description", skills=[],
                    location="Remote", remote_type="REMOTE", attributes={}))
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id, profile_id=profile_id,
                    profile_version="v1", score=90, missing=[], components={}))
                connection.execute(m.routes.insert().values(
                    id=f"route-{job_id}", job_id=job_id, company_id=company_id,
                    provider="greenhouse", tenant=f"tenant-{job_id}",
                    requisition=f"REQ-{job_id}",
                    url=f"https://apply.example.test/{job_id}?token=route-secret"))
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}", job_id=job_id, source="feed",
                    source_job_id=f"source-{job_id}",
                    source_url=f"https://feed.example.test/{job_id}?key=posting-secret",
                    application_url="https://apply.example.test", raw={"secret": "raw-secret"},
                    fetched_at="2026-10-10T12:00:00Z"))
                source_id = f"source-{job_id}"
                connection.execute(m.company_sources.insert().values(
                    id=source_id, company_id=company_id, provider="greenhouse",
                    tenant=f"tenant-{job_id}", endpoint="https://feed.example.test?token=provider-secret",
                    discovered_automatically=True))
                connection.execute(m.posting_snapshots.insert().values(
                    id=f"snapshot-{job_id}", posting_id=f"posting-{job_id}",
                    raw={"password": "snapshot-secret"}, sha256="a" * 64,
                    retrieved_at="2026-10-10T12:00:00Z"))
                connection.execute(m.source_quality_observations.insert().values(
                    id=f"quality-{job_id}", company_source_id=source_id,
                    observed_at="2026-10-10T12:00:00Z", succeeded=True))
                connection.execute(m.posting_value_candidates.insert().values(
                    id=f"candidate-{job_id}", posting_id=f"posting-{job_id}",
                    posting_snapshot_id=f"snapshot-{job_id}", company_source_id=source_id,
                    quality_observation_id=f"quality-{job_id}",
                    observed_at="2026-10-10T12:00:00Z", normalized={},
                    application_url="https://apply.example.test"))
            connection.execute(m.applications.insert().values(
                id="application-a", job_id="job-a", route_id="route-job-a",
                profile_id="fixture", profile_version="v1", state="DRAFT",
                target="MOCK", fact_ids=["private-fact"],
                trace={"answer": "private-answer"}, document_version=1,
                confirmation=None, created_at="2026-10-10T12:00:00Z", submitted_at=None))
            for profile_id, company_id, source_id in (
                    ("fixture", "company-a", "source-job-a"),
                    ("other", "company-b", "source-job-b")):
                connection.execute(m.audit.insert().values(
                    id=f"discovered-{profile_id}", entity_id=profile_id,
                    action="COMPANY_DISCOVERED", payload={"company_id": company_id},
                    timestamp="2026-10-10T12:00:00Z"))
                connection.execute(m.audit.insert().values(
                    id=f"enrolled-{profile_id}", entity_id=profile_id,
                    action="COMPANY_SOURCE_ENROLLED",
                    payload={"company_id": company_id, "company_source_id": source_id},
                    timestamp="2026-10-10T12:00:00Z"))

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_persisted_company_source_job_route_application_graph_is_read_only(self):
        before = self._snapshot()
        response = self.client.get("/profiles/fixture/source-relationship-graph")

        self.assertEqual(response.status_code, 200, response.text)
        graph = response.json()
        self.assertEqual(graph["profile_id"], "fixture")
        self.assertEqual({node["id"] for node in graph["nodes"]}, {
            "company:company-a", "company_job_source:source-job-a", "canonical_job:job-a",
            "job_posting:posting-job-a", "application_route:route-job-a", "application:application-a",
        })
        self.assertEqual({(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]}, {
            ("company_source", "company:company-a", "company_job_source:source-job-a"),
            ("source_posting", "company_job_source:source-job-a", "job_posting:posting-job-a"),
            ("job_posting", "canonical_job:job-a", "job_posting:posting-job-a"),
            ("job_route", "canonical_job:job-a", "application_route:route-job-a"),
            ("route_application", "application_route:route-job-a", "application:application-a"),
        })
        for secret in ("provider-secret", "route-secret", "posting-secret", "raw-secret",
                       "snapshot-secret", "private-answer", "private-fact", "Private description"):
            self.assertNotIn(secret, json.dumps(graph, sort_keys=True))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.client.post("/profiles/fixture/source-relationship-graph").status_code, 405)

    def test_graph_is_profile_isolated_and_empty_profiles_return_empty_arrays(self):
        other = self.client.get("/profiles/other/source-relationship-graph")
        empty = self.client.get("/profiles/empty/source-relationship-graph")

        self.assertEqual(other.status_code, 200, other.text)
        other_ids = {node["id"] for node in other.json()["nodes"]}
        self.assertIn("company:company-b", other_ids)
        self.assertIn("canonical_job:job-b", other_ids)
        self.assertNotIn("company:company-a", other_ids)
        self.assertNotIn("canonical_job:job-a", other_ids)
        self.assertNotIn("application:application-a", other_ids)
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), {"profile_id": "empty", "nodes": [], "edges": []})

    def test_missing_profile_is_not_treated_as_an_empty_graph(self):
        response = self.client.get("/profiles/missing/source-relationship-graph")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"], {"code": "SOURCE_RELATIONSHIP_GRAPH_PROFILE_NOT_FOUND"})


if __name__ == "__main__":
    unittest.main()
