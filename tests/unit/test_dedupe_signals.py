import unittest

from services.dedupe.signals import (
    canonical_application_url,
    company_identity_signals,
    dedupe_title_key,
    description_similarity,
    duplicate_candidate_evidence,
    strong_identity_signals,
)


class DedupeSignalTests(unittest.TestCase):
    def test_application_url_canonicalization_removes_tracking_only(self):
        left = canonical_application_url(
            "HTTPS://ATS.EXAMPLE.COM/jobs/123/?utm_source=board&foo=bar#apply"
        )
        right = canonical_application_url(
            "https://ats.example.com/jobs/123?foo=bar"
        )
        self.assertEqual(left, right)
        self.assertEqual(left, "https://ats.example.com/jobs/123?foo=bar")

    def test_company_name_alone_never_establishes_identity(self):
        result = company_identity_signals(
            {"name": "Acme Corporation"},
            {"name": "Acme, Inc."},
        )
        self.assertFalse(result["same_company"])
        self.assertTrue(result["name_overlap_only"])
        self.assertIn("NAME_OR_ALIAS", result["signals"])
    def test_company_domain_or_ats_metadata_corroborates_alias(self):
        result = company_identity_signals(
            {"name": "Acme", "domains": ["acme.example"], "ats_tenants": ["acme"]},
            {"name": "Acme Corporation", "domains": ["ACME.EXAMPLE"], "ats_tenants": ["other"]},
        )
        self.assertTrue(result["same_company"])
        self.assertIn("DOMAIN", result["signals"])
        self.assertIn("NAME_OR_ALIAS", result["signals"])

    def test_title_key_normalizes_case_spacing_punctuation_and_known_abbreviation(self):
        self.assertEqual(dedupe_title_key("WINDOWS   SYSADMIN"), dedupe_title_key("Windows Systems Administrator"))
        self.assertNotEqual(dedupe_title_key("Database Administrator"), dedupe_title_key("Systems Administrator"))

    def test_description_similarity_uses_exact_hash_before_fuzzy(self):
        exact = description_similarity(
            "Build Python systems. Manage Active Directory.",
            " build   python systems; manage active directory ",
        )
        self.assertTrue(exact["exact"])
        self.assertEqual(exact["method"], "NORMALIZED_HASH")
        self.assertEqual(exact["similarity"], 1.0)

        fuzzy = description_similarity(
            "Build Python systems and manage Active Directory for enterprise users.",
            "Build Python services and manage Active Directory for business users.",
        )
        self.assertFalse(fuzzy["exact"])
        self.assertEqual(fuzzy["method"], "TRIGRAM_JACCARD")
        self.assertGreater(fuzzy["similarity"], 0.5)
    def test_strong_signals_cover_route_requisition_and_source_relationship(self):
        left = {
            "application_url": "https://ats.example/jobs/123?utm_source=a",
            "ats_tenant": "acme",
            "requisition": "123",
            "employer_requisition": "IT-123",
            "source_relationships": [{"source": "board-a", "source_job_id": "abc"}],
        }
        right = {
            "application_url": "https://ats.example/jobs/123",
            "ats_tenant": "ACME",
            "requisition": "123",
            "employer_requisition": "it-123",
            "source_relationships": [{"source": "BOARD-A", "source_job_id": "ABC"}],
        }
        self.assertEqual(
            set(strong_identity_signals(left, right)),
            {"APPLICATION_URL", "ATS_TENANT_REQUISITION", "EMPLOYER_REQUISITION", "SOURCE_JOB_RELATIONSHIP"},
        )

    def test_ats_requisition_signal_is_independent_of_changed_route(self):
        left = {
            "application_url": "https://board-a.example/jobs/old",
            "ats_tenant": "acme",
            "requisition": "123",
        }
        right = {
            "application_url": "https://board-b.example/jobs/new",
            "ats_tenant": "ACME",
            "requisition": "123",
        }

        self.assertEqual(strong_identity_signals(left, right), ["ATS_TENANT_REQUISITION"])

    def test_duplicate_candidate_requires_company_corroboration_for_strong_match(self):
        common = {
            "title": "Windows Sysadmin",
            "description": "Manage Windows infrastructure and PowerShell.",
            "application_url": "https://ats.example/jobs/123",
            "ats_tenant": "acme",
            "requisition": "123",
        }
        left = {**common, "company": {"name": "Acme", "domains": ["acme.example"]}}
        right = {**common, "company": {"name": "Acme Corp", "domains": ["acme.example"]}}
        evidence = duplicate_candidate_evidence(left, right)
        self.assertTrue(evidence["strong_match"])
        self.assertTrue(evidence["title"]["equal"])
        self.assertEqual(evidence["description"]["similarity"], 1.0)

        unrelated = {**common, "company": {"name": "Acme", "domains": ["other.example"]}}
        evidence = duplicate_candidate_evidence(left, unrelated)
        self.assertFalse(evidence["strong_match"])


if __name__ == "__main__":
    unittest.main()
