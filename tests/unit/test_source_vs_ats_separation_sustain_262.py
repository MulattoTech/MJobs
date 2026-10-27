"""Sustain distinct discovery-source and ATS/application provenance."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.sources import (
    register_ats_tenant,
    upsert_company_job_source,
)
from services.discovery.sources.collect import configure_source
from services.operations.source_relationship_graph import read_source_relationship_graph
from tests.integration.test_spine import PROFILE


class SourceVsAtsSeparationSustainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "source-vs-ats.db"
        self.database_url = f"sqlite:///{path.as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other-profile", "email": "other@example.test",
        })
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert(), [
                {"id": "acme", "name": "Acme", "domain": "acme.example.test"},
                {"id": "other", "name": "Other", "domain": "other.example.test"},
            ])

        self.ats = register_ats_tenant(
            self.store,
            "acme",
            provider="greenhouse",
            tenant_identifier="acme-board",
            confidence=0.99,
            evidence=["Synthetic fixture ATS evidence"],
        )
        self.other_ats = register_ats_tenant(
            self.store,
            "other",
            provider="greenhouse",
            tenant_identifier="other-board",
            confidence=0.99,
            evidence=["Synthetic fixture ATS evidence"],
        )
        self.sources = [
            upsert_company_job_source(
                self.store,
                company_id="acme",
                provider="greenhouse",
                tenant_identifier="acme-board",
                career_url=f"https://acme.example.test/careers/{portal}",
                public_api_url=f"https://boards-api.greenhouse.io/v1/boards/acme-board/jobs?portal={portal}",
                collection_strategy="API",
                discovered_automatically=True,
                ats_tenant_id=self.ats["id"],
                profile_id="fixture",
            )
            for portal in ("engineering", "operations")
        ]
        self._seed_visible_job_graph()

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _seed_visible_job_graph(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.insert().values(
                id="job-acme", company_id="acme", requisition="REQ-1",
                title="Platform Engineer", description="Synthetic fixture role",
                skills=[], location="Remote", remote_type="REMOTE", attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id="match-acme", job_id="job-acme", profile_id="fixture",
                profile_version="v1", score=90, missing=[], components={},
            ))
            connection.execute(m.routes.insert().values(
                id="route-acme", job_id="job-acme", company_id="acme",
                provider="greenhouse", tenant="acme-board", requisition="REQ-1",
                url="https://boards.greenhouse.io/acme-board/jobs/REQ-1",
            ))
            for index, (source, discovery_source) in enumerate(zip(
                    self.sources, ("hiringcafe", "company-careers")), start=1):
                posting_id = f"posting-{index}"
                snapshot_id = f"snapshot-{index}"
                quality_id = f"quality-{index}"
                connection.execute(m.postings.insert().values(
                    id=posting_id, job_id="job-acme", source=discovery_source,
                    source_job_id=f"{discovery_source}-REQ-1",
                    source_url=f"https://{discovery_source}.example.test/jobs/REQ-1",
                    application_url="https://boards.greenhouse.io/acme-board/jobs/REQ-1",
                    raw={"fixture": True}, fetched_at="2026-10-24T12:00:00Z",
                ))
                connection.execute(m.posting_snapshots.insert().values(
                    id=snapshot_id, posting_id=posting_id, raw={"fixture": True},
                    sha256=str(index) * 64, retrieved_at="2026-10-24T12:00:00Z",
                ))
                connection.execute(m.source_quality_observations.insert().values(
                    id=quality_id, company_source_id=source["id"],
                    source_endpoint=source["endpoint"], observed_at="2026-10-24T12:00:00Z",
                    succeeded=True, available=True, enumerated=1, accepted=1,
                ))
                connection.execute(m.posting_value_candidates.insert().values(
                    id=f"candidate-{index}", posting_id=posting_id,
                    posting_snapshot_id=snapshot_id, company_source_id=source["id"],
                    quality_observation_id=quality_id,
                    observed_at="2026-10-24T12:00:00Z", normalized={},
                    application_url="https://boards.greenhouse.io/acme-board/jobs/REQ-1",
                ))

    def test_postings_preserve_discovery_source_separately_from_application_ats(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            graph = read_source_relationship_graph(self.store, "fixture")

        nodes = {node["id"]: node for node in graph["nodes"]}
        postings = [node for node in graph["nodes"] if node["type"] == "job_posting"]
        routes = [node for node in graph["nodes"] if node["type"] == "application_route"]
        source_edges = {
            (edge["source"], edge["target"])
            for edge in graph["edges"] if edge["type"] == "source_posting"
        }

        self.assertEqual(
            {node["metadata"]["source"] for node in postings},
            {"hiringcafe", "company-careers"},
        )
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["metadata"]["provider"], "greenhouse")
        self.assertEqual(routes[0]["metadata"]["tenant"], "acme-board")
        self.assertEqual({source["ats_tenant_id"] for source in self.sources}, {self.ats["id"]})
        self.assertEqual(len({source["id"] for source in self.sources}), 2)
        self.assertEqual(source_edges, {
            (f"company_job_source:{self.sources[0]['id']}", "job_posting:posting-1"),
            (f"company_job_source:{self.sources[1]['id']}", "job_posting:posting-2"),
        })
        for source in self.sources:
            source_node = nodes[f"company_job_source:{source['id']}"]
            self.assertEqual(source_node["metadata"]["provider"], "greenhouse")
            self.assertEqual(source_node["metadata"]["tenant"], "acme-board")

    def test_repeats_are_stable_and_profile_scope_prevents_cross_company_edges(self):
        first = read_source_relationship_graph(self.store, "fixture")
        self.assertEqual(first, read_source_relationship_graph(self.store, "fixture"))
        fixture_ids = {node["id"] for node in first["nodes"]}
        self.assertNotIn("company:other", fixture_ids)
        self.assertNotIn(f"company_job_source:{self.other_ats['id']}", fixture_ids)
        self.assertEqual(
            read_source_relationship_graph(self.store, "other-profile"),
            {"nodes": [], "edges": []},
        )

    def test_missing_or_cross_company_ats_bindings_fail_without_writes(self):
        with self.store.engine.connect() as connection:
            before_sources = [dict(row) for row in connection.execute(
                select(m.company_sources).order_by(m.company_sources.c.id)
            ).mappings()]
            before_tenants = [dict(row) for row in connection.execute(
                select(m.company_ats_tenants).order_by(m.company_ats_tenants.c.id)
            ).mappings()]
            before_audit = [dict(row) for row in connection.execute(
                select(m.audit).order_by(m.audit.c.id)
            ).mappings()]
            other_sources = list(connection.execute(select(m.company_sources.c.id).where(
                m.company_sources.c.company_id == "other"
            )).scalars())

        # A detected ATS tenant alone never invents a discovery-source edge.
        self.assertIn(self.other_ats["id"], {row["id"] for row in before_tenants})
        self.assertEqual(other_sources, [])

        attempts = (
            {"ats_tenant_id": "missing-tenant", "tenant_identifier": "acme-board"},
            {"ats_tenant_id": self.other_ats["id"], "tenant_identifier": "other-board"},
        )
        for index, attempt in enumerate(attempts, start=1):
            with self.subTest(attempt=index), self.assertRaisesRegex(
                    ValueError, "COMPANY_SOURCE_ATS_TENANT_MISMATCH"):
                upsert_company_job_source(
                    self.store,
                    company_id="acme",
                    provider="greenhouse",
                    tenant_identifier=attempt["tenant_identifier"],
                    career_url=f"https://acme.example.test/review/{index}",
                    public_api_url=f"https://boards-api.greenhouse.io/v1/boards/review-{index}/jobs",
                    collection_strategy="API",
                    discovered_automatically=True,
                    ats_tenant_id=attempt["ats_tenant_id"],
                    profile_id="fixture",
                )

        with self.store.engine.connect() as connection:
            after_sources = [dict(row) for row in connection.execute(
                select(m.company_sources).order_by(m.company_sources.c.id)
            ).mappings()]
            after_tenants = [dict(row) for row in connection.execute(
                select(m.company_ats_tenants).order_by(m.company_ats_tenants.c.id)
            ).mappings()]
            after_audit = [dict(row) for row in connection.execute(
                select(m.audit).order_by(m.audit.c.id)
            ).mappings()]
        self.assertEqual(after_sources, before_sources)
        self.assertEqual(after_tenants, before_tenants)
        self.assertEqual(after_audit, before_audit)

    def test_public_filters_and_graph_keep_direct_employer_distinct_after_sqlite_reopen(self):
        # Public source filtering uses the collector source ID on the job;
        # ATS filtering requires exact company/provider/tenant evidence.
        verified_source_id = configure_source(
            self.store, "synthetic-catalog", provider="remoteok")
        direct_source_id = configure_source(
            self.store, "direct-employer-catalog", provider="remoteok")
        direct_company_source = upsert_company_job_source(
            self.store,
            company_id="other",
            provider="company-careers",
            tenant_identifier="direct-careers",
            career_url="https://other.example.test/careers?credential=private-source-key",
            collection_strategy="HTML",
            discovered_automatically=False,
            ats_tenant_id=None,
            profile_id="fixture",
        )
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == "job-acme").values(
                attributes={"source_id": verified_source_id}))
            connection.execute(m.jobs.insert().values(
                id="job-direct", company_id="other", requisition="REQ-DIRECT",
                title="Direct employer role", description="Synthetic direct-employer fixture",
                skills=[], location="Remote", remote_type="REMOTE",
                attributes={"source_id": direct_source_id},
            ))
            connection.execute(m.matches.insert().values(
                id="match-direct", job_id="job-direct", profile_id="fixture",
                profile_version="v1", score=80, missing=[], components={},
            ))
            # The tenant name is verified for Acme only. Matching provider and
            # tenant text on another company must not inherit that identity.
            connection.execute(m.routes.insert().values(
                id="route-direct", job_id="job-direct", company_id="other",
                provider="greenhouse", tenant="acme-board", requisition="REQ-DIRECT",
                url="https://boards.greenhouse.io/acme-board/jobs/REQ-DIRECT?token=route-secret",
            ))
            connection.execute(m.postings.insert().values(
                id="posting-direct", job_id="job-direct", source="company-careers",
                source_job_id="direct-REQ-DIRECT",
                source_url="https://other.example.test/careers/REQ-DIRECT?key=posting-secret",
                application_url="https://boards.greenhouse.io/acme-board/jobs/REQ-DIRECT",
                raw={"private": "raw-posting-secret"}, fetched_at="2026-10-24T12:00:00Z",
            ))
            connection.execute(m.posting_snapshots.insert().values(
                id="snapshot-direct", posting_id="posting-direct",
                raw={"private": "snapshot-secret"}, sha256="d" * 64,
                retrieved_at="2026-10-24T12:00:00Z",
            ))
            connection.execute(m.source_quality_observations.insert().values(
                id="quality-direct", company_source_id=direct_company_source["id"],
                source_endpoint=direct_company_source["endpoint"],
                observed_at="2026-10-24T12:00:00Z", succeeded=True,
                available=True, enumerated=1, accepted=1,
            ))
            connection.execute(m.posting_value_candidates.insert().values(
                id="candidate-direct", posting_id="posting-direct",
                posting_snapshot_id="snapshot-direct",
                company_source_id=direct_company_source["id"],
                quality_observation_id="quality-direct",
                observed_at="2026-10-24T12:00:00Z", normalized={},
                application_url="https://boards.greenhouse.io/acme-board/jobs/REQ-DIRECT",
            ))

        # Reopen the real SQLite file before exercising public serialization and
        # filters; no in-memory fixture state may be required for provenance.
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", Path(self.temp.name),
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))
        try:
            verified_source = client.get("/jobs", params={
                "job_source_id": verified_source_id,
            })
            self.assertEqual(verified_source.status_code, 200, verified_source.text)
            self.assertEqual([row["id"] for row in verified_source.json()["jobs"]], ["job-acme"])

            source_only = client.get("/jobs", params={"job_source_id": direct_source_id})
            self.assertEqual(source_only.status_code, 200, source_only.text)
            self.assertEqual([row["id"] for row in source_only.json()["jobs"]], ["job-direct"])

            route_provider = client.get("/jobs", params={"provider": "greenhouse"})
            self.assertEqual(route_provider.status_code, 200, route_provider.text)
            self.assertEqual({row["id"] for row in route_provider.json()["jobs"]},
                             {"job-acme", "job-direct"})

            verified_ats = client.get("/jobs", params={"ats_provider": "greenhouse"})
            self.assertEqual(verified_ats.status_code, 200, verified_ats.text)
            self.assertEqual([row["id"] for row in verified_ats.json()["jobs"]], ["job-acme"])
            source_without_ats = client.get("/jobs", params={
                "job_source_id": direct_source_id, "ats_provider": "greenhouse",
            })
            self.assertEqual(source_without_ats.status_code, 200, source_without_ats.text)
            self.assertEqual(source_without_ats.json()["jobs"], [])

            first_response = client.get("/profiles/fixture/source-relationship-graph")
            second_response = client.get("/profiles/fixture/source-relationship-graph")
            self.assertEqual(first_response.status_code, 200, first_response.text)
            self.assertEqual(first_response.content, second_response.content)
            graph = first_response.json()
            nodes = {node["id"]: node for node in graph["nodes"]}
            direct_source_node = nodes[f"company_job_source:{direct_company_source['id']}"]
            direct_route_node = nodes["application_route:route-direct"]
            direct_posting_node = nodes["job_posting:posting-direct"]
            self.assertEqual(direct_source_node["metadata"]["provider"], "company-careers")
            self.assertEqual(direct_source_node["metadata"]["origin"], "USER_WATCHED")
            self.assertEqual(direct_route_node["metadata"]["provider"], "greenhouse")
            self.assertEqual(direct_route_node["metadata"]["tenant"], "acme-board")
            self.assertEqual(direct_posting_node["metadata"]["source"], "company-careers")
            with self.store.engine.connect() as connection:
                direct_source_row = connection.execute(select(m.company_sources).where(
                    m.company_sources.c.id == direct_company_source["id"]
                )).mappings().one()
            self.assertIsNone(direct_source_row["ats_tenant_id"])
            edges = {(edge["type"], edge["source"], edge["target"])
                     for edge in graph["edges"]}
            self.assertIn(("source_posting", direct_source_node["id"], direct_posting_node["id"]), edges)
            self.assertIn(("job_route", "canonical_job:job-direct", direct_route_node["id"]), edges)
            self.assertFalse(any(
                source == direct_source_node["id"] and target == direct_route_node["id"]
                for _, source, target in edges
            ))
            self.assertEqual(len({node["id"] for node in graph["nodes"]}), len(graph["nodes"]))
            self.assertEqual(len({edge["id"] for edge in graph["edges"]}), len(graph["edges"]))
            serialized = json.dumps(graph, sort_keys=True)
            for private_value in (
                    "private-source-key", "route-secret", "posting-secret",
                    "raw-posting-secret", "snapshot-secret"):
                self.assertNotIn(private_value, serialized)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
