"""Durable employer ATS tenant, source, and company relationship tests."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import (
    link_company_parent,
    register_ats_tenant,
    upsert_company_job_source,
)


class CompanySourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "acme", "name": "Acme Holdings", "domain": ""})
            upsert(connection, m.companies, {"id": "acme-federal", "name": "Acme Federal", "domain": ""})
            upsert(connection, m.companies, {"id": "acme-unit", "name": "Acme Unit", "domain": ""})

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_tenant_and_multiple_company_sources_are_persisted_independently(self):
        tenant = register_ats_tenant(
            self.store, "acme", provider="greenhouse", tenant_identifier="acme",
            organization_identifier="acme-holdings", board_identifier="engineering",
            confidence=0.97, evidence=["GREENHOUSE_BOARD_LINK"],
        )
        corporate = upsert_company_job_source(
            self.store, company_id="acme", provider="greenhouse", tenant_identifier="acme",
            career_url="https://careers.acme.example/engineering",
            public_api_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            collection_strategy="PUBLIC_API", confidence=0.97, discovered_automatically=True,
            ats_tenant_id=tenant["id"],
        )
        federal = upsert_company_job_source(
            self.store, company_id="acme", provider="icims", tenant_identifier="acme-federal",
            career_url="https://careers.acme.example/federal",
            search_url="https://careers.acme.example/federal/search",
            collection_strategy="SEARCH_PAGE", confidence=0.82, discovered_automatically=False,
        )
        canonical_duplicate = upsert_company_job_source(
            self.store, company_id="acme", provider="greenhouse", tenant_identifier="acme",
            career_url="https://Careers.Acme.example:443/engineering/#openings",
            public_api_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            collection_strategy="PUBLIC_API", confidence=0.97, discovered_automatically=True,
            ats_tenant_id=tenant["id"],
        )

        with self.store.engine.connect() as connection:
            rows = list(connection.execute(select(m.company_sources).where(
                m.company_sources.c.company_id == "acme"
            )).mappings())
            stored_tenants = list(connection.execute(select(m.company_ats_tenants).where(
                m.company_ats_tenants.c.company_id == "acme"
            )).mappings())

        self.assertEqual({row["id"] for row in rows}, {corporate["id"], federal["id"]})
        self.assertEqual(canonical_duplicate["id"], corporate["id"])
        self.assertEqual({row["career_url"] for row in rows}, {
            "https://careers.acme.example/engineering", "https://careers.acme.example/federal",
        })
        self.assertEqual({row["provider"] for row in rows}, {"greenhouse", "icims"})
        self.assertEqual(stored_tenants[0]["organization_identifier"], "acme-holdings")
        self.assertEqual(stored_tenants[0]["board_identifier"], "engineering")
        self.assertEqual(corporate["ats_tenant_id"], tenant["id"])

    def test_source_upsert_preserves_success_timestamp_when_no_new_success_is_recorded(self):
        first = upsert_company_job_source(
            self.store, company_id="acme", provider="rss", tenant_identifier="acme",
            career_url="https://careers.acme.example/feed/",
            feed_url="https://careers.acme.example/feed/jobs.xml", collection_strategy="RSS",
            discovered_automatically=True, last_success="2026-10-01T12:00:00+00:00",
        )
        repeated = upsert_company_job_source(
            self.store, company_id="acme", provider="rss", tenant_identifier="acme",
            career_url="https://careers.acme.example/feed",
            feed_url="https://careers.acme.example/feed/jobs.xml", collection_strategy="RSS",
            discovered_automatically=True,
        )

        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(repeated["last_success"], "2026-10-01T12:00:00+00:00")

    def test_parent_relationship_does_not_merge_company_identity(self):
        relationship = link_company_parent(self.store, "acme", "acme-federal", "SUBSIDIARY")

        with self.store.engine.connect() as connection:
            parent = connection.execute(select(m.companies).where(m.companies.c.id == "acme-federal")).mappings().one()
            original_parent = connection.execute(select(m.companies).where(m.companies.c.id == "acme")).mappings().one()

        self.assertEqual(relationship["parent_company_id"], "acme")
        self.assertEqual(parent["parent_company_id"], "acme")
        self.assertEqual(parent["relationship_type"], "SUBSIDIARY")
        self.assertIsNone(original_parent["parent_company_id"])
        self.assertNotEqual(parent["id"], original_parent["id"])

    def test_company_cannot_be_its_own_parent(self):
        with self.assertRaisesRegex(ValueError, "COMPANY_PARENT_RELATIONSHIP_INVALID"):
            link_company_parent(self.store, "acme", "acme", "SUBSIDIARY")

    def test_company_parent_relationships_reject_direct_and_indirect_cycles(self):
        link_company_parent(self.store, "acme", "acme-federal", "SUBSIDIARY")
        link_company_parent(self.store, "acme-federal", "acme-unit", "SUBSIDIARY")

        with self.assertRaisesRegex(ValueError, "COMPANY_PARENT_RELATIONSHIP_INVALID"):
            link_company_parent(self.store, "acme-unit", "acme", "SUBSIDIARY")


if __name__ == "__main__":
    unittest.main()
