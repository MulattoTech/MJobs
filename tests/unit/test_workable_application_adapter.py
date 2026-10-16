import unittest
from unittest.mock import patch

from services.applications.adapters.workable import WorkableApplicationAdapter


class WorkableApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = WorkableApplicationAdapter()

    def test_normalizes_workable_application_route(self):
        self.assertEqual(self.adapter.normalize_route({
            "provider": "workable",
            "url": "https://apply.workable.com/ExampleCo/j/ABC123DEF/?source=fixture",
        }), {
            "provider": "workable",
            "tenant": "exampleco",
            "requisition": "abc123def",
            "url": "https://apply.workable.com/exampleco/j/abc123def",
        })

    def test_rejects_untrusted_or_malformed_routes(self):
        invalid_routes = (
            {"provider": "lever", "url": "https://apply.workable.com/example/j/abc123"},
            {"provider": "workable", "url": "http://apply.workable.com/example/j/abc123"},
            {"provider": "workable", "url": "https://apply.workable.com.attacker.test/example/j/abc123"},
            {"provider": "workable", "url": "https://user@apply.workable.com/example/j/abc123"},
            {"provider": "workable", "url": "https://apply.workable.com:443/example/j/abc123"},
            {"provider": "workable", "url": "https://apply.workable.com/example%2fother/j/abc123"},
            {"provider": "workable", "url": "https://apply.workable.com/example/j/abc123/extra"},
            {"provider": "workable", "url": "https://apply.workable.com/example/j/abc$123"},
            {"provider": "workable", "url": "https://apply.workable.com/example/j/abc123#fragment"},
        )
        for route in invalid_routes:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_rejects_conflicting_tenant_or_requisition_metadata(self):
        url = "https://apply.workable.com/example/j/abc123"
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "workable", "url": url, "tenant": "other",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "workable", "url": url, "requisition": "other",
        }))

    def test_capabilities_declare_only_review_boundary(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_known_standard_contact_field_can_be_reviewed(self):
        analysis = {"status": "PASS", "fields": [{
            "tag": "input", "input_type": "email", "name": "email",
            "labels": ["Email"], "sensitivity": "STANDARD", "ambiguous": False,
        }]}
        self.assertFalse(self.adapter.requires_human_review(analysis))

    def test_unknown_sensitive_ambiguous_and_challenge_fields_fail_closed(self):
        cases = (
            {"status": "UNKNOWN", "fields": []},
            {"status": "PASS", "fields": [{
                "name": "custom_question", "labels": ["Why do you want this job?"],
                "sensitivity": "STANDARD", "ambiguous": False,
            }]},
            {"status": "PASS", "fields": [{
                "name": "gender", "labels": ["Gender"],
                "sensitivity": "SENSITIVE", "ambiguous": False,
            }]},
            {"status": "PASS", "fields": [{
                "name": "email", "labels": ["Email"],
                "sensitivity": "STANDARD", "ambiguous": True,
            }]},
            {"status": "PASS", "fields": [{
                "name": "recaptcha_email", "labels": ["Email"],
                "sensitivity": "STANDARD", "ambiguous": False,
            }]},
        )
        for analysis in cases:
            with self.subTest(analysis=analysis):
                self.assertTrue(self.adapter.requires_human_review(analysis))

    @patch("services.applications.adapters.workable.analyze_form_page")
    def test_detects_embedded_challenge_and_fails_closed(self, analyzer):
        analyzer.return_value = {"status": "PASS", "fields": [{
            "tag": "input", "input_type": "email", "name": "email",
            "labels": ["Email"], "sensitivity": "STANDARD", "ambiguous": False,
        }]}

        class Locator:
            def count(self):
                return 1

        class Page:
            def locator(self, selector):
                self.selector = selector
                return Locator()

        page = Page()
        self.assertEqual(self.adapter.analyze_page(page), {
            "status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED_FORM", "fields": [],
        })
        self.assertIn("captcha", page.selector)

    @patch("services.applications.adapters.workable.analyze_form_page")
    def test_analyze_page_uses_shared_value_free_analyzer(self, analyzer):
        page = object()
        expected = {"status": "UNKNOWN", "reason": "NO_FORM", "fields": []}
        analyzer.return_value = expected
        self.assertEqual(self.adapter.analyze_page(page), expected)
        analyzer.assert_called_once_with(page)


if __name__ == "__main__":
    unittest.main()
