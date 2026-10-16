import unittest
from unittest.mock import patch

from services.applications.adapters.recruitee import RecruiteeApplicationAdapter


class RecruiteeApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecruiteeApplicationAdapter()

    def test_normalizes_tenant_job_route_and_discards_tracking_query(self):
        self.assertEqual(self.adapter.normalize_route({
            "provider": "Recruitee",
            "url": "https://Example.recruitee.com/o/software-engineer?utm_source=fixture",
        }), {
            "provider": "recruitee",
            "tenant": "example",
            "requisition": "software-engineer",
            "url": "https://example.recruitee.com/o/software-engineer",
        })

    def test_rejects_untrusted_hosts_and_malformed_routes(self):
        invalid = (
            {"provider": "lever", "url": "https://example.recruitee.com/o/job"},
            {"provider": "recruitee", "url": "http://example.recruitee.com/o/job"},
            {"provider": "recruitee", "url": "https://example.recruitee.com.attacker.test/o/job"},
            {"provider": "recruitee", "url": "https://recruitee.com/o/job"},
            {"provider": "recruitee", "url": "https://a.b.recruitee.com/o/job"},
            {"provider": "recruitee", "url": "https://user@example.recruitee.com/o/job"},
            {"provider": "recruitee", "url": "https://example.recruitee.com:8443/o/job"},
            {"provider": "recruitee", "url": "https://example.recruitee.com/o/job/extra"},
            {"provider": "recruitee", "url": "https://example.recruitee.com/o/job%2fother"},
            {"provider": "recruitee", "url": "https://example.recruitee.com/jobs/job"},
            {"provider": "recruitee", "url": "https://example.recruitee.com/o/job#apply"},
        )
        for route in invalid:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_rejects_conflicting_route_metadata(self):
        url = "https://example.recruitee.com/o/software-engineer"
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "recruitee", "url": url, "tenant": "another",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "recruitee", "url": url, "requisition": "different-job",
        }))

    def test_capabilities_do_not_claim_upload_interaction_or_submission(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_known_standard_contact_fields_pass_review_gate(self):
        analysis = {"status": "PASS", "fields": [
            {"tag": "input", "name": "first_name", "labels": ["First Name"],
             "sensitivity": "STANDARD", "ambiguous": False},
            {"tag": "input", "name": "email", "labels": ["Email address"],
             "sensitivity": "STANDARD", "ambiguous": False},
        ]}
        self.assertFalse(self.adapter.requires_human_review(analysis))

    def test_unknown_sensitive_ambiguous_and_challenge_fields_require_review(self):
        base = {"tag": "input", "sensitivity": "STANDARD", "ambiguous": False}
        for analysis in (
            {"status": "UNKNOWN", "fields": []},
            None,
            {"status": "PASS", "fields": [{**base, "name": "custom_q_42"}]},
            {"status": "PASS", "fields": [{**base, "name": "first_name", "sensitivity": "SENSITIVE"}]},
            {"status": "PASS", "fields": [{**base, "name": "email", "ambiguous": True}]},
            {"status": "PASS", "fields": [{**base, "name": "recaptcha_response"}]},
            {"status": "PASS", "fields": [{**base, "tag": "textarea", "name": "first_name"}]},
        ):
            with self.subTest(analysis=analysis):
                self.assertTrue(self.adapter.requires_human_review(analysis))

    @patch("services.applications.adapters.greenhouse.analyze_form_page")
    def test_page_analysis_uses_shared_value_free_analyzer(self, analyzer):
        analyzer.return_value = {"status": "UNKNOWN", "reason": "NO_FORM", "fields": []}
        class ChallengeLocator:
            def count(self):
                return 0
        class Page:
            def locator(self, selector):
                self.selector = selector
                return ChallengeLocator()
        page = Page()
        self.assertEqual(self.adapter.analyze_page(page), analyzer.return_value)
        analyzer.assert_called_once_with(page)

    @patch("services.applications.adapters.greenhouse.analyze_form_page")
    def test_challenge_widget_fails_closed_without_returning_page_data(self, analyzer):
        analyzer.return_value = {"status": "PASS", "fields": [{
            "tag": "input", "name": "email", "labels": ["Email"],
            "sensitivity": "STANDARD", "ambiguous": False,
        }]}
        class ChallengeLocator:
            def count(self):
                return 1
        class Page:
            def locator(self, selector):
                return ChallengeLocator()
        self.assertEqual(self.adapter.analyze_page(Page()), {
            "status": "UNKNOWN", "reason": "CHALLENGE_PRESENT", "fields": [],
        })


if __name__ == "__main__":
    unittest.main()
