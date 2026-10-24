"""Additional MJ-REQ-216 regression coverage for ambiguous company aliases."""
from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine

from packages.persistence import models as m
from services.company_discovery.resolution import resolve_company


class CompanyResolutionSustainTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        tables = (m.companies, m.company_aliases, m.company_domains,
                  m.company_ats_tenants, m.company_sources,
                  m.company_discovery_requests)
        m.metadata.create_all(self.engine, tables=tables)
        self.store = SimpleNamespace(engine=self.engine)

    def add_company(self, company_id, name):
        with self.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name=name, domain=""))

    def add_verified_alias(self, company_id, alias):
        with self.engine.begin() as connection:
            connection.execute(m.company_aliases.insert().values(
                id=f"alias-{company_id}", company_id=company_id,
                alias=alias, normalized_alias="starlight labs", alias_type="BRAND",
                confidence=.96, verified=True, source="SYNTHETIC_FIXTURE",
                evidence=["Synthetic first-party brand declaration"],
                created_at="2026-10-24T00:00:00Z"))

    def test_shared_verified_alias_does_not_choose_unrelated_company(self):
        self.add_company("starlight-parent", "Starlight Holdings")
        self.add_company("unrelated", "Unrelated Analytics")
        self.add_verified_alias("starlight-parent", "Starlight Labs")
        self.add_verified_alias("unrelated", "Starlight Labs")

        result = resolve_company(self.store, {"company_name": "Starlight Labs"})

        self.assertTrue(result["is_new"])
        self.assertNotIn(result["company_id"], {"starlight-parent", "unrelated"})
        self.assertEqual(result["confidence"], .45)
        self.assertIn("AMBIGUOUS_COMPANY_MATCH", result["evidence"])
        self.assertEqual(result["name"], "Starlight Labs")
