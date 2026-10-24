"""Sustain distinct discovery-source and ATS/application provenance."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.sources import (
    register_ats_tenant,
    upsert_company_job_source,
)
from services.operations.source_relationship_graph import read_source_relationship_graph
from tests.integration.test_spine import PROFILE


class SourceVsAtsSeparationSustainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "source-vs-ats.db"
        self.store = Store(f"sqlite:///{path.as_posix()}")
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


if __name__ == "__main__":
    unittest.main()
