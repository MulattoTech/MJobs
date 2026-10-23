"""MJ-REQ-216: deterministic identity evidence and non-merging controls.

Exercise the real resolver over isolated input tables. No migrations,
network, API server, or external employer data are needed.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select

from packages.persistence import models as m
from services.company_discovery.resolution import resolve_company
from services.company_discovery.sources import register_ats_tenant, upsert_company_job_source


class CompanyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        self.tables = (m.companies, m.company_aliases, m.company_domains,
                       m.company_ats_tenants, m.company_sources, m.company_discovery_requests)
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
                    verification_method="FIRST_PARTY_JOB_LINK",
                    first_seen_at="2026-10-01T00:00:00Z",
                    last_verified_at="2026-10-01T00:00:00Z" if verified else None,
                    evidence=["Synthetic official career link"])

    def untrusted_ats(self, company_id, tenant="northstar", provider="greenhouse"):
        self.insert(m.company_sources, id="ats-" + company_id, company_id=company_id,
                    provider=provider, tenant=tenant,
                    endpoint="https://ats.example/" + tenant,
                    discovered_automatically=False)

    def ats(self, company_id, tenant="northstar", provider="greenhouse"):
        # These are the same tenant/source registration helpers used by public
        # ATS enrollment; raw source observations are not identity authority.
        career_url = "https://" + company_id + ".example/careers"
        self.domain(company_id, company_id + ".example")
        with self.engine.begin() as connection:
            tenant_row = register_ats_tenant(
                connection, company_id, provider=provider, tenant_identifier=tenant,
                confidence=.99, evidence=["Synthetic verified career page: " + career_url])
            return upsert_company_job_source(
                connection, company_id=company_id, provider=provider,
                tenant_identifier=tenant, career_url=career_url,
                public_api_url="https://ats.example/" + tenant,
                collection_strategy="API", confidence=.99, enabled=True,
                ats_tenant_id=tenant_row["id"],
                last_validation="2026-10-01T00:00:00Z")

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
        result = self.resolve(ats_tenant="northstar", company_website="https://first.example")
        self.assert_existing(result, "first", .99,
                             ["VERIFIED_DOMAIN_MATCH", "ATS_TENANT_MATCH"])

    def test_untrusted_ats_source_cannot_override_verified_application_domain(self):
        self.company("verified-employer", "Northstar Robotics")
        self.company("untrusted-employer", "Unrelated Finance")
        self.domain("verified-employer")
        self.untrusted_ats("untrusted-employer")
        for label, overrides in (
            ("unlinked-low-confidence", {}),
            ("disabled", {"enabled": False}),
            ("automatic-unreviewed", {"discovered_automatically": True}),
            ("high-confidence-without-authorization", {"confidence": .99}),
        ):
            with self.subTest(source=label):
                with self.engine.begin() as connection:
                    connection.execute(m.company_sources.update().values(
                        **{"enabled": True, "confidence": .45,
                           "discovered_automatically": False, **overrides}))
                result = self.resolve(ats_tenant="northstar",
                                      application_url="https://northstar.example/jobs/1")
                self.assert_existing(result, "verified-employer", .90,
                                     ["VERIFIED_DOMAIN_MATCH"])

    def test_enrolled_source_loses_tenant_authority_when_disabled_or_unvalidated(self):
        self.company("known", "Northstar Holdings")
        source = self.ats("known")
        for label, changes in (
            ("disabled", {"enabled": False}),
            ("low-source-confidence", {"confidence": .45}),
            ("unvalidated", {"last_validation": None}),
            ("unlinked", {"ats_tenant_id": None}),
            ("changed-provider", {"provider": "lever"}),
            ("changed-tenant", {"tenant": "other"}),
            ("unverified-career-host", {"career_url": "https://unverified.example/careers"}),
            ("malformed-authorization", {"identity_authorization": {"kind": "UNTRUSTED"}}),
            ("missing-review-request", {"identity_authorization": {
                "kind": "USER_CONFIRMED_DOMAIN", "website": "https://known.example/",
                "request_id": "missing"}}),
        ):
            with self.subTest(source=label):
                with self.engine.begin() as connection:
                    connection.execute(m.company_sources.update().values(**{**source, **changes}))
                result = self.resolve(ats_tenant=changes.get("tenant", "northstar"))
                self.assertTrue(result["is_new"])
                self.assertNotIn("ATS_TENANT_MATCH", result["evidence"])

    def test_tenant_record_must_be_evidenced_and_belong_to_source_company(self):
        self.company("known", "Northstar Holdings")
        self.company("other", "Unrelated Finance")
        self.ats("known")
        with self.engine.connect() as connection:
            original = dict(connection.execute(select(m.company_ats_tenants)).mappings().one())
        for changes in ({"confidence": .45}, {"evidence": []}, {"company_id": "other"}):
            with self.subTest(changes=changes):
                with self.engine.begin() as connection:
                    connection.execute(m.company_ats_tenants.update().values(**{**original, **changes}))
                result = self.resolve(ats_tenant="northstar")
                self.assertTrue(result["is_new"])
                self.assertNotIn("ATS_TENANT_MATCH", result["evidence"])

    def test_automatic_enrolled_source_remains_valid_with_current_domain_proof(self):
        self.company("known", "Northstar Holdings")
        self.ats("known")
        with self.engine.begin() as connection:
            connection.execute(m.company_sources.update().values(discovered_automatically=True))
        self.assert_existing(self.resolve(ats_tenant="northstar"), "known", .95,
                             ["ATS_TENANT_MATCH"])

    def test_revoked_or_ambiguous_career_domain_cannot_authorize_tenant(self):
        self.company("known", "Northstar Holdings")
        self.company("other", "Unrelated Finance")
        self.ats("known")
        with self.engine.connect() as connection:
            original = dict(connection.execute(select(m.company_domains)).mappings().one())
        for changes in ({"verified": False}, {"evidence": []},
                        {"verification_method": "CANDIDATE_DOMAIN_UNVERIFIED"}):
            with self.subTest(changes=changes):
                with self.engine.begin() as connection:
                    connection.execute(m.company_domains.update().values(**{**original, **changes}))
                self.assertTrue(self.resolve(ats_tenant="northstar")["is_new"])
        with self.engine.begin() as connection:
            connection.execute(m.company_domains.update().values(**original))
        self.domain("other", "known.example")
        self.assertTrue(self.resolve(ats_tenant="northstar")["is_new"])

    def test_resolution_is_repeatable_and_does_not_persist_candidate_evidence(self):
        self.company("known", "Northstar Holdings")
        self.ats("known")
        with self.engine.connect() as connection:
            before = {table.name: connection.execute(select(table)).all() for table in self.tables}
        with patch("socket.socket", side_effect=AssertionError("Resolution must not use network")):
            first = self.resolve(company_website="https://unverified.example")
            self.assertEqual(first, self.resolve(company_website="https://unverified.example"))
            self.assert_existing(self.resolve(ats_tenant="northstar"), "known", .95,
                                 ["ATS_TENANT_MATCH"])
        with self.engine.connect() as connection:
            after = {table.name: connection.execute(select(table)).all() for table in self.tables}
        self.assertEqual(before, after)
