import unittest
from unittest.mock import patch

from services.applications.adapters.smartrecruiters import SmartRecruitersApplicationAdapter


class SmartRecruitersApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SmartRecruitersApplicationAdapter()

    def test_normalizes_canonical_smartrecruiters_job_route(self):
        normalized = self.adapter.normalize_route({
            "provider": "smartrecruiters",
            "url": "https://jobs.smartrecruiters.com/Example/743999123456789-Software-Engineer?source=fixture",
        })

        self.assertEqual(normalized, {
            "provider": "smartrecruiters",
            "tenant": "example",
            "requisition": "743999123456789",
            "url": "https://jobs.smartrecruiters.com/example/743999123456789-Software-Engineer",
        })

    def test_rejects_untrusted_hosts_and_non_job_route_shapes(self):
        invalid = (
            {"provider": "lever", "url": "https://jobs.smartrecruiters.com/example/743999123456789"},
            {"provider": "smartrecruiters", "url": "http://jobs.smartrecruiters.com/example/743999123456789"},
            {"provider": "smartrecruiters", "url": "https://jobs.smartrecruiters.com.attacker.test/example/743999123456789"},
            {"provider": "smartrecruiters", "url": "https://user@jobs.smartrecruiters.com/example/743999123456789"},
            {"provider": "smartrecruiters", "url": "https://jobs.smartrecruiters.com:444/example/743999123456789"},
            {"provider": "smartrecruiters", "url": "https://jobs.smartrecruiters.com/example/job/743999123456789"},
            {"provider": "smartrecruiters", "url": "https://jobs.smartrecruiters.com/example/not-a-job-id"},
            {"provider": "smartrecruiters", "url": "https://jobs.smartrecruiters.com/example/743999123456789/extra"},
            {"provider": "smartrecruiters", "url": "https://jobs.smartrecruiters.com/example/743999123456789#apply"},
        )

        for route in invalid:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_rejects_route_metadata_that_conflicts_with_url(self):
        url = "https://jobs.smartrecruiters.com/example/743999123456789-role"
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "smartrecruiters", "url": url, "tenant": "other",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "smartrecruiters", "url": url, "requisition": "743999123456788",
        }))

    def test_declares_only_review_and_metadata_capabilities(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_unknown_sensitive_and_challenge_protected_forms_require_review(self):
        self.assertTrue(self.adapter.requires_human_review({"status": "UNKNOWN", "fields": []}))
        self.assertTrue(self.adapter.requires_human_review({"status": "PASS", "fields": [
            {"sensitivity": "SENSITIVE", "ambiguous": False},
        ]}))
        self.assertTrue(self.adapter.requires_human_review({"status": "PASS", "fields": [
            {"sensitivity": "STANDARD", "ambiguous": True},
        ]}))
        self.assertFalse(self.adapter.requires_human_review({"status": "PASS", "fields": [
            {"sensitivity": "STANDARD", "ambiguous": False},
        ]}))

    @patch("services.applications.adapters.smartrecruiters.analyze_form_page")
    def test_analyzes_only_allowlisted_origin_and_detects_challenge_markers(self, analyze):
        class Locator:
            def __init__(self, count):
                self._count = count
            def count(self):
                return self._count

        class Page:
            url = "https://jobs.smartrecruiters.com/example/743999123456789-role"
            challenge_count = 0
            def locator(self, selector):
                return Locator(self.challenge_count if "captcha" in selector else 0)

        page = Page()
        analyze.return_value = {"status": "PASS", "reason": None, "fields": [
            {"sensitivity": "STANDARD", "ambiguous": False},
        ]}
        self.assertEqual(self.adapter.analyze_page(page)["status"], "PASS")
        analyze.assert_called_once_with(page)

        page.challenge_count = 1
        self.assertEqual(self.adapter.analyze_page(page), {
            "status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED_FORM", "fields": [],
        })
        analyze.reset_mock()

        page.challenge_count = 0
        page.url = "https://attacker.test/example/743999123456789-role"
        self.assertEqual(self.adapter.analyze_page(page), {
            "status": "UNKNOWN", "reason": "UNTRUSTED_FORM_ORIGIN", "fields": [],
        })
        analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
