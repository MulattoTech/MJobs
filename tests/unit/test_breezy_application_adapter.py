"""Synthetic contract tests for the inspection-only Breezy adapter."""
import unittest

from services.applications.adapters.breezy import BreezyApplicationAdapter


class BreezyApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BreezyApplicationAdapter()

    def test_normalizes_public_job_and_apply_routes(self):
        route = self.adapter.normalize_route({
            "provider": "BREEZY",
            "url": "https://Acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-Software-Engineer/apply?source=fixture",
        })
        self.assertEqual(route, {
            "provider": "breezy",
            "tenant": "acme",
            "requisition": "123e4567-e89b-12d3-a456-426614174000",
            "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-software-engineer",
        })

    def test_rejects_foreign_malformed_and_ambiguous_routes(self):
        invalid = (
            None,
            {"provider": "lever", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "http://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://acme.breezy.hr.attacker.test/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://user@acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://acme.breezy.hr:443/p/123e4567-e89b-12d3-a456-426614174000-job"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/not-a-position"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job/unknown"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job/"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job#apply"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job", "tenant": "other"},
            {"provider": "breezy", "url": "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-job", "requisition": "other"},
        )
        for route in invalid:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_capabilities_are_inspection_only(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_known_basic_contact_fixture_needs_no_extra_review(self):
        analysis = {"status": "PASS", "fields": [{
            "tag": "input", "input_type": "email", "sensitivity": "STANDARD", "ambiguous": False,
            "labels": ["Email address"],
        }]}
        self.assertFalse(self.adapter.requires_human_review(analysis))

    def test_select_and_textarea_controls_require_review_even_with_contact_labels(self):
        for tag in ("select", "textarea"):
            with self.subTest(tag=tag):
                analysis = {"status": "PASS", "fields": [{
                    "tag": tag, "input_type": None, "sensitivity": "STANDARD",
                    "ambiguous": False, "labels": ["Email address"],
                }]}
                self.assertTrue(self.adapter.requires_human_review(analysis))

    def test_unknown_sensitive_ambiguous_and_unsupported_controls_fail_closed(self):
        analyses = (
            None,
            {"status": "UNKNOWN", "fields": []},
            {"status": "PASS", "fields": [{
                "input_type": "text", "sensitivity": "STANDARD", "ambiguous": False,
                "labels": ["Why do you want to work here?"],
            }]},
            {"status": "PASS", "fields": [{
                "input_type": "password", "sensitivity": "CREDENTIAL", "ambiguous": False,
                "labels": ["Email address"],
            }]},
            {"status": "PASS", "fields": [{
                "input_type": "email", "sensitivity": "STANDARD", "ambiguous": True,
                "labels": ["Email address"],
            }]},
            {"status": "PASS", "fields": [{
                "input_type": "checkbox", "sensitivity": "STANDARD", "ambiguous": False,
                "labels": ["Email address"],
            }]},
        )
        for analysis in analyses:
            with self.subTest(analysis=analysis):
                self.assertTrue(self.adapter.requires_human_review(analysis))


if __name__ == "__main__":
    unittest.main()
