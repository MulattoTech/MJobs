"""MJ-REQ-216: deterministic identity evidence and non-merging controls.

Exercise the real resolver over only its four input tables. No migrations,
network, API server, or external employer data are needed.
"""
from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine, select

from packages.persistence import models as m
from services.company_discovery.resolution import resolve_company


class CompanyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        self.tables = (m.companies, m.company_aliases, m.company_domains, m.company_sources)
        m.metadata.create_all(self.engine, tables=self.tables)
        self.store = SimpleNamespace(engine=self.engine)

    def insert(self, table, **values):
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**values))

    def company(self, identifier, name, **extra):
        self.insert(m.companies, id=identifier, name=name, domain="", **extra)

    def alias(self, company_id, *, verified=True, confidence=.93):
        self.insert(m.company_aliases, id="alias-" + company_id,
                    company_id=company_id, alias="Northstar Labs",
                    normalized_alias="northstar labs", alias_type="BRAND",
                    confidence=confidence, verified=verified, source="SYNTHETIC_FIXTURE",
                    evidence=["Synthetic first-party brand declaration"],
                    created_at="2026-10-01T00:00:00Z")

    def domain(self, company_id, host="northstar.example", *, verified=True):
        self.insert(m.company_domains, id="domain-" + company_id, company_id=company_id,
                    domain=host, confidence=.99, verified=verified,
                    verification_method="FIRST_PARTY_CAREER_LINK",
                    first_seen_at="2026-10-01T00:00:00Z",
                    last_verified_at="2026-10-01T00:00:00Z" if verified else None,
                    evidence=["Synthetic official career link"])

    def ats(self, company_id, tenant="northstar", provider="greenhouse"):
        self.insert(m.company_sources, id="ats-" + company_id, company_id=company_id,
                    provider=provider, tenant=tenant,
                    endpoint="https://ats.example/" + tenant,
                    discovered_automatically=False)

    def resolve(self, name="Unrelated Display Name", **evidence):
        return resolve_company(self.store, {"company_name": name, **evidence})

    def assert_existing(self, result, company_id, confidence, evidence):
        self.assertFalse(result["is_new"])
        self.assertEqual(result["company_id"], company_id)
        self.assertEqual(result["confidence"], confidence)
        self.assertEqual(result["evidence"], evidence)

    def test_verified_brand_alias_resolves_canonical_company(self):
        self.company("parent", "Northstar Holdings")
        self.alias("parent")
        result = self.resolve("NORTHSTAR LABS")
        self.assert_existing(result, "parent", .93, ["VERIFIED_ALIAS_EXACT"])
        self.assertEqual(result["name"], "Northstar Holdings")

    def test_unverified_alias_does_not_merge_even_with_high_confidence(self):
        self.company("parent", "Northstar Holdings")
        self.alias("parent", verified=False, confidence=1.0)
        result = self.resolve("Northstar Labs")
        self.assertTrue(result["is_new"])
        self.assertNotEqual(result["company_id"], "parent")
        self.assertEqual(result["evidence"], ["NORMALIZED_NAME_ONLY"])

    def test_punctuation_case_spacing_and_legal_suffix_are_name_evidence_only(self):
        self.company("known", "Northstar Data, Inc.")
        for spelling in ("NORTHSTAR DATA", " Northstar   Data LLC ", "Northstar-Data Ltd."):
            with self.subTest(spelling=spelling):
                self.assert_existing(self.resolve(spelling), "known", .75,
                                     ["NORMALIZED_NAME_EXACT"])

    def test_subsidiary_name_keeps_child_identity_not_parent(self):
        self.company("parent", "Northstar Holdings")
        self.company("child", "Northstar Robotics", parent_company_id="parent",
                     relationship_type="SUBSIDIARY")
        self.assert_existing(self.resolve("Northstar Robotics Inc."), "child", .75,
                             ["NORMALIZED_NAME_EXACT"])
        self.assert_existing(self.resolve("Northstar Holdings"), "parent", .75,
                             ["NORMALIZED_NAME_EXACT"])

    def test_subsidiary_domain_does_not_resolve_to_parent(self):
        self.company("parent", "Northstar Holdings")
        self.company("child", "Northstar Robotics", parent_company_id="parent",
                     relationship_type="SUBSIDIARY")
        self.domain("parent", "holding.example")
        self.domain("child", "robotics.example")
        result = self.resolve(company_website="https://careers.robotics.example/jobs")
        self.assert_existing(result, "child", .99, ["VERIFIED_DOMAIN_MATCH"])

    def test_similar_unrelated_names_are_not_fuzzy_merged(self):
        self.company("known", "Northstar Data")
        for name in ("Northstar Dataworks", "North Star Data", "Northstar Analytics"):
            with self.subTest(name=name):
                result = self.resolve(name)
                self.assertTrue(result["is_new"])
                self.assertNotEqual(result["company_id"], "known")
                self.assertEqual(result["evidence"], ["NORMALIZED_NAME_ONLY"])

    def test_same_normalized_name_for_two_companies_remains_ambiguous(self):
        self.company("first", "Northstar Data Inc.")
        self.company("second", "Northstar Data LLC")
        result = self.resolve("Northstar Data")
        self.assertTrue(result["is_new"])
        self.assertNotIn(result["company_id"], {"first", "second"})
        self.assertEqual(result["confidence"], .45)
        self.assertIn("AMBIGUOUS_COMPANY_MATCH", result["evidence"])

    def test_verified_domain_and_true_subdomain_resolve(self):
        self.company("known", "Northstar Holdings")
        self.domain("known")
        for host in ("northstar.example", "careers.northstar.example"):
            with self.subTest(host=host):
                result = self.resolve(company_website="https://" + host + "/jobs")
                self.assert_existing(result, "known", .99, ["VERIFIED_DOMAIN_MATCH"])
                self.assertEqual(result["domain"], "northstar.example")

    def test_lookalike_domain_suffixes_are_not_verified_evidence(self):
        self.company("known", "Northstar Holdings")
        self.domain("known")
        for host in ("notnorthstar.example", "northstar.example.attacker.example"):
            with self.subTest(host=host):
                result = self.resolve(company_website="https://" + host)
                self.assertTrue(result["is_new"])
                self.assertNotEqual(result["company_id"], "known")
                self.assertEqual(result["evidence"], ["CANDIDATE_DOMAIN_UNVERIFIED"])

    def test_unverified_domain_cannot_resolve_a_different_name(self):
        self.company("known", "Northstar Holdings")
        self.domain("known", verified=False)
        result = self.resolve(company_website="https://northstar.example")
        self.assertTrue(result["is_new"])
        self.assertNotEqual(result["company_id"], "known")
        self.assertEqual(result["evidence"], ["CANDIDATE_DOMAIN_UNVERIFIED"])

    def test_application_domain_confidence_is_capped(self):
        self.company("known", "Northstar Holdings")
        self.domain("known")
        self.assert_existing(self.resolve(application_url="https://northstar.example/jobs/1"),
                             "known", .90, ["VERIFIED_DOMAIN_MATCH"])

    def test_aggregator_domain_is_not_company_identity_even_if_recorded_verified(self):
        self.company("known", "Northstar Holdings")
        self.domain("known", "linkedin.com")
        result = self.resolve(company_website="https://linkedin.com/company/other")
        self.assertTrue(result["is_new"])
        self.assertEqual(result["evidence"], ["UNTRUSTED_AGGREGATOR_DOMAIN"])
        self.assertEqual(result["domain"], "")

    def test_ats_tenant_match_is_case_insensitive_and_exact(self):
        self.company("known", "Northstar Holdings")
        self.ats("known")
        self.assert_existing(self.resolve(ats_tenant=" NORTHSTAR "), "known", .95,
                             ["ATS_TENANT_MATCH"])
        for tenant in ("northstar-other", "", "unrelated"):
            with self.subTest(tenant=tenant):
                result = self.resolve(ats_tenant=tenant)
                self.assertTrue(result["is_new"])
                self.assertNotIn("ATS_TENANT_MATCH", result["evidence"])

    def test_duplicate_ats_tenants_do_not_choose_an_arbitrary_company(self):
        self.company("first", "Northstar Robotics")
        self.company("second", "Unrelated Finance")
        self.ats("first")
        self.ats("second", provider="lever")
        result = self.resolve(ats_tenant="northstar")
        self.assertTrue(result["is_new"])
        self.assertNotIn(result["company_id"], {"first", "second"})
        self.assertIn("AMBIGUOUS_COMPANY_MATCH", result["evidence"])

    def test_verified_domain_disambiguates_shared_ats_tenant(self):
        self.company("first", "Northstar Robotics")
        self.company("second", "Unrelated Finance")
        self.ats("first")
        self.ats("second", provider="lever")
        self.domain("first")
        result = self.resolve(ats_tenant="northstar", company_website="https://northstar.example")
        self.assert_existing(result, "first", .99,
                             ["VERIFIED_DOMAIN_MATCH", "ATS_TENANT_MATCH"])

    def test_resolution_is_repeatable_and_does_not_persist_candidate_evidence(self):
        self.company("known", "Northstar Holdings")
        with self.engine.connect() as connection:
            before = {table.name: connection.execute(select(table)).all() for table in self.tables}
        first = self.resolve(company_website="https://unverified.example")
        self.assertEqual(first, self.resolve(company_website="https://unverified.example"))
        with self.engine.connect() as connection:
            after = {table.name: connection.execute(select(table)).all() for table in self.tables}
        self.assertEqual(before, after)
