from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SourceRelationshipGraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'graph.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        self.store.save_profile({**deepcopy(PROFILE), "id": "empty", "email": "empty@example.test"})
        with self.store.engine.begin() as connection:
            for company_id, name in (("company-a", "Alpha Systems"), ("company-b", "Beta Systems")):
                connection.execute(m.companies.insert().values(
                    id=company_id, name=name, domain=f"{company_id}.example.test"))
            for company_id, job_id, title in (
                    ("company-a", "job-a", "Platform Engineer"),
                    ("company-b", "job-b", "Network Engineer")):
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id=company_id, requisition=f"REQ-{job_id}",
                    title=title, description="Private description", skills=[],
                    location="Remote", remote_type="REMOTE", attributes={}))
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id,
                    profile_id="fixture" if job_id == "job-a" else "other",
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
                    tenant=f"tenant-{job_id}",
                    endpoint="https://feed.example.test?token=provider-secret",
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

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_graph_preserves_scoped_relationships_and_excludes_private_metadata(self):
        try:
            from services.operations.source_relationship_graph import read_source_relationship_graph
        except ImportError as error:
            self.fail(f"source relationship graph query is missing: {error}")

        graph = read_source_relationship_graph(self.store, "fixture")
        node_ids = {node["id"] for node in graph["nodes"]}
        edges = {(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]}
        self.assertEqual(node_ids, {
            "company:company-a", "company_job_source:source-job-a", "canonical_job:job-a",
            "job_posting:posting-job-a", "application_route:route-job-a", "application:application-a",
        })
        self.assertEqual(edges, {
            ("company_source", "company:company-a", "company_job_source:source-job-a"),
            ("source_posting", "company_job_source:source-job-a", "job_posting:posting-job-a"),
            ("job_posting", "canonical_job:job-a", "job_posting:posting-job-a"),
            ("job_route", "canonical_job:job-a", "application_route:route-job-a"),
            ("route_application", "application_route:route-job-a", "application:application-a"),
        })
        serialized = json.dumps(graph, sort_keys=True)
        for secret in ("provider-secret", "route-secret", "posting-secret", "raw-secret",
                       "snapshot-secret", "private-answer", "private-fact", "Private description"):
            self.assertNotIn(secret, serialized)

    def test_graph_is_profile_scoped_sorted_and_empty_profiles_return_empty_graphs(self):
        try:
            from services.operations.source_relationship_graph import read_source_relationship_graph
        except ImportError as error:
            self.fail(f"source relationship graph query is missing: {error}")

        first = read_source_relationship_graph(self.store, "fixture")
        self.assertEqual(first, read_source_relationship_graph(self.store, "fixture"))
        other = read_source_relationship_graph(self.store, "other")
        other_ids = {node["id"] for node in other["nodes"]}
        self.assertIn("company:company-b", other_ids)
        self.assertIn("canonical_job:job-b", other_ids)
        self.assertNotIn("company:company-a", other_ids)
        self.assertNotIn("canonical_job:job-a", other_ids)
        self.assertEqual(read_source_relationship_graph(self.store, "empty"), {"nodes": [], "edges": []})


if __name__ == "__main__":
    unittest.main()
