import unittest
from unittest.mock import patch

from services.applications.adapters.lever import LeverApplicationAdapter


class LeverApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = LeverApplicationAdapter()

    def test_normalizes_supported_public_lever_job_routes(self):
        normalized = self.adapter.normalize_route({
            "provider": "lever",
            "url": "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000/apply?source=fixture",
        })

        self.assertEqual(normalized, {
            "provider": "lever",
            "tenant": "example",
            "requisition": "123e4567-e89b-12d3-a456-426614174000",
            "url": "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000",
        })

    def test_normalizes_eu_lever_routes_to_the_canonical_host(self):
        normalized = self.adapter.normalize_route({
            "provider": "lever",
            "url": "https://jobs.eu.lever.co/Example/123E4567-E89B-12D3-A456-426614174000",
        })

        self.assertEqual(normalized["tenant"], "example")
        self.assertEqual(normalized["url"],
                         "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000")

    def test_rejects_untrusted_or_malformed_routes(self):
        invalid_routes = (
            {"provider": "greenhouse", "url": "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000"},
            {"provider": "lever", "url": "http://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000"},
            {"provider": "lever", "url": "https://jobs.lever.co.attacker.test/example/123e4567-e89b-12d3-a456-426614174000"},
            {"provider": "lever", "url": "https://user@jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000"},
            {"provider": "lever", "url": "https://jobs.lever.co:443/example/123e4567-e89b-12d3-a456-426614174000"},
            {"provider": "lever", "url": "https://jobs.lever.co/example/not-a-posting-id"},
            {"provider": "lever", "url": "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000/unknown"},
            {"provider": "lever", "url": "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000/"},
            {"provider": "lever", "url": "https://jobs.lever.co/example%2fother/123e4567-e89b-12d3-a456-426614174000"},
        )

        for route in invalid_routes:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_rejects_supplied_tenant_or_requisition_that_conflicts_with_url(self):
        url = "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000"
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "lever", "url": url, "tenant": "other",
        }))
        self.assertIsNone(self.adapter.normalize_route({
            "provider": "lever", "url": url, "requisition": "other",
        }))

    def test_capabilities_are_explicit_and_keep_live_actions_disabled(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_sensitive_ambiguous_and_unknown_fields_require_human_review(self):
        standard = {"status": "PASS", "fields": [
            {"sensitivity": "STANDARD", "ambiguous": False, "name": "first_name"},
        ]}
        sensitive = {"status": "PASS", "fields": [
            {"sensitivity": "SENSITIVE", "ambiguous": False},
        ]}
        ambiguous = {"status": "PASS", "fields": [
            {"sensitivity": "STANDARD", "ambiguous": True},
        ]}

        self.assertFalse(self.adapter.requires_human_review(standard))
        self.assertTrue(self.adapter.requires_human_review(sensitive))
        self.assertTrue(self.adapter.requires_human_review(ambiguous))
        self.assertTrue(self.adapter.requires_human_review({"status": "UNKNOWN", "fields": []}))

    def test_unknown_custom_question_requires_human_review(self):
        analysis = {"status": "PASS", "fields": [{
            "sensitivity": "STANDARD", "ambiguous": False, "tag": "textarea",
            "id": "custom_q_982", "name": "custom_q_982",
            "labels": ["What should we know about you?"],
        }]}

        self.assertTrue(self.adapter.requires_human_review(analysis))

    @patch("services.applications.adapters.greenhouse.analyze_form_page")
    def test_analyze_page_reuses_the_shared_value_free_analyzer(self, analyzer):
        page = object()
        expected = {"status": "UNKNOWN", "reason": "NO_FORM", "fields": []}
        analyzer.return_value = expected

        self.assertEqual(self.adapter.analyze_page(page), expected)
        analyzer.assert_called_once_with(page)


if __name__ == "__main__":
    unittest.main()
