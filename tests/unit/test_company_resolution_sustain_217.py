"""MJ-REQ-216 regression coverage for evidence-based company resolution."""
from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine

from packages.persistence import models as m
from services.company_discovery.aliases import normalize_alias
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

    def insert(self, table, **values):
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**values))

    def add_company(self, company_id, name, **extra):
        self.insert(m.companies, id=company_id, name=name, domain="", **extra)

    def add_alias(self, company_id, alias, *, verified=True, confidence=.96):
        normalized = normalize_alias(alias)
        self.insert(
            m.company_aliases,
            id=f"alias-{company_id}-{normalized.replace(' ', '-')}",
            company_id=company_id, alias=alias, normalized_alias=normalized,
            alias_type="BRAND", confidence=confidence, verified=verified,
            source="SYNTHETIC_FIXTURE",
            evidence=["Synthetic first-party brand declaration"],
            created_at="2026-10-24T00:00:00Z",
        )

    def add_domain(self, company_id, host, *, verified=True):
        self.insert(
            m.company_domains, id=f"domain-{company_id}-{host}",
            company_id=company_id, domain=host, confidence=.99,
            verified=verified, verification_method="FIRST_PARTY_JOB_LINK",
            first_seen_at="2026-10-24T00:00:00Z",
            last_verified_at="2026-10-24T00:00:00Z" if verified else None,
            evidence=["Synthetic official career link"],
        )

    def add_ats_source(self, company_id, tenant, *, provider="greenhouse"):
        self.insert(
            m.company_sources, id=f"ats-{company_id}-{provider}",
            company_id=company_id, provider=provider, tenant=tenant,
            endpoint=f"https://ats.example/{tenant}",
            discovered_automatically=False,
        )

    def resolve(self, name="Unrelated Display Name", **evidence):
        return resolve_company(self.store, {"company_name": name, **evidence})

    def assert_existing(self, result, company_id, confidence, evidence):
        self.assertFalse(result["is_new"])
        self.assertEqual(result["company_id"], company_id)
        self.assertEqual(result["confidence"], confidence)
        self.assertEqual(result["evidence"], evidence)

    def test_verified_alias_and_punctuation_variants_resolve_without_fuzzy_matching(self):
        self.add_company("known", "Northstar Data, Inc.")
        self.add_alias("known", "Northstar Labs")
        self.add_company("unverified-brand", "Starlight Holdings")
        self.add_alias("unverified-brand", "Starlight Labs", verified=False)

        self.assert_existing(self.resolve("NORTHSTAR LABS"), "known", .96,
                             ["VERIFIED_ALIAS_EXACT"])
        for spelling in ("NORTHSTAR DATA", " Northstar   Data LLC ",
                         "Northstar-Data Ltd."):
            with self.subTest(spelling=spelling):
                self.assert_existing(self.resolve(spelling), "known", .75,
                                     ["NORMALIZED_NAME_EXACT"])

        unverified = self.resolve("Starlight Labs")
        self.assertTrue(unverified["is_new"])
        self.assertNotEqual(unverified["company_id"], "unverified-brand")
        self.assertEqual(unverified["evidence"], ["NORMALIZED_NAME_ONLY"])

    def test_subsidiary_name_and_domain_preserve_child_identity(self):
        self.add_company("parent", "Northstar Holdings")
        self.add_company("child", "Northstar Robotics", parent_company_id="parent",
                         relationship_type="SUBSIDIARY")
        self.add_domain("parent", "holdings.example")
        self.add_domain("child", "robotics.example")

        child = self.resolve("Northstar Robotics Inc.",
                             company_website="https://careers.robotics.example/jobs")
        self.assert_existing(child, "child", .99,
                             ["VERIFIED_DOMAIN_MATCH", "NORMALIZED_NAME_EXACT"])
        self.assertEqual(child["name"], "Northstar Robotics")
        self.assert_existing(self.resolve("Northstar Holdings"), "parent", .75,
                             ["NORMALIZED_NAME_EXACT"])

    def test_similarly_named_unrelated_companies_are_not_fuzzy_merged(self):
        self.add_company("known", "Northstar Data")
        for name in ("Northstar Dataworks", "North Star Data", "Northstar Analytics"):
            with self.subTest(name=name):
                result = self.resolve(name)
                self.assertTrue(result["is_new"])
                self.assertNotEqual(result["company_id"], "known")
                self.assertEqual(result["evidence"], ["NORMALIZED_NAME_ONLY"])

    def test_verified_domain_and_subdomain_match_but_lookalike_or_unverified_do_not(self):
        self.add_company("known", "Northstar Holdings")
        self.add_domain("known", "northstar.example")
        self.add_domain("unverified", "unverified.example", verified=False)
        self.add_company("unverified", "Other Employer")

        for host in ("northstar.example", "careers.northstar.example"):
            with self.subTest(host=host):
                self.assert_existing(
                    self.resolve(company_website=f"https://{host}/jobs"),
                    "known", .99, ["VERIFIED_DOMAIN_MATCH"])

        for host in ("northstar.example.attacker.example", "unverified.example"):
            with self.subTest(host=host):
                result = self.resolve(company_website=f"https://{host}/jobs")
                self.assertTrue(result["is_new"])
                self.assertNotEqual(result["company_id"], "known")
                self.assertEqual(result["evidence"], ["CANDIDATE_DOMAIN_UNVERIFIED"])

    def test_exact_ats_tenant_resolves_but_unknown_or_ambiguous_tenant_does_not(self):
        self.add_company("known", "Northstar Holdings")
        self.add_ats_source("known", "northstar")

        self.assert_existing(self.resolve("Unrelated Display Name", ats_tenant=" NORTHSTAR "),
                             "known", .95, ["ATS_TENANT_MATCH"])
        unknown = self.resolve("Unrelated Display Name", ats_tenant="northstar-other")
        self.assertTrue(unknown["is_new"])
        self.assertNotIn("ATS_TENANT_MATCH", unknown["evidence"])

        self.add_company("other", "Different Employer")
        self.add_ats_source("other", "northstar", provider="lever")
        ambiguous = self.resolve("Unrelated Display Name", ats_tenant="northstar")
        self.assertTrue(ambiguous["is_new"])
        self.assertNotIn(ambiguous["company_id"], {"known", "other"})
        self.assertIn("AMBIGUOUS_COMPANY_MATCH", ambiguous["evidence"])

    def test_shared_verified_alias_does_not_choose_unrelated_company(self):
        self.add_company("starlight-parent", "Starlight Holdings")
        self.add_company("unrelated", "Unrelated Analytics")
        self.add_alias("starlight-parent", "Starlight Labs")
        self.add_alias("unrelated", "Starlight Labs")

        result = self.resolve("Starlight Labs")

        self.assertTrue(result["is_new"])
        self.assertNotIn(result["company_id"], {"starlight-parent", "unrelated"})
        self.assertEqual(result["confidence"], .45)
        self.assertIn("AMBIGUOUS_COMPANY_MATCH", result["evidence"])
        self.assertEqual(result["name"], "Starlight Labs")
