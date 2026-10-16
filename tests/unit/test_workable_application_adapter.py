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
            "visible": True, "editable": True, "interactive": True,
        }]}
        self.assertFalse(self.adapter.requires_human_review(analysis))

    def test_hidden_email_input_requires_human_review(self):
        analysis = {"status": "PASS", "fields": [{
            "tag": "input", "input_type": "hidden", "name": "email",
            "labels": ["Email"], "sensitivity": "STANDARD", "ambiguous": False,
            "visible": False, "editable": False, "interactive": False,
        }]}
        self.assertTrue(self.adapter.requires_human_review(analysis))

    def test_contact_field_without_proven_interactive_state_requires_review(self):
        states = (
            {},
            {"visible": False, "editable": True, "interactive": True},
            {"visible": True, "editable": False, "interactive": True},
            {"visible": True, "editable": True, "interactive": False},
        )
        for state in states:
            with self.subTest(state=state):
                field = {
                    "tag": "input", "input_type": "email", "name": "email",
                    "labels": ["Email"], "sensitivity": "STANDARD", "ambiguous": False,
                    **state,
                }
                self.assertTrue(self.adapter.requires_human_review({
                    "status": "PASS", "fields": [field],
                }))

    @patch("services.applications.adapters.workable.analyze_form_page")
    def test_analyze_page_adds_value_free_control_interactivity(self, analyzer):
        analyzer.return_value = {"status": "PASS", "fields": [{
            "index": 0, "tag": "input", "input_type": "email", "name": "email",
            "id": "email", "labels": ["Email"], "sensitivity": "STANDARD",
            "ambiguous": False,
        }]}

        class ChallengeLocator:
            def count(self):
                return 0

        class ControlsLocator:
            def evaluate_all(self, script, **kwargs):
                return [{
                    "tag": "input", "input_type": "email", "name": "email", "id": "email",
                    "visible": True, "editable": True, "interactive": True,
                }]

        class Page:
            def locator(self, selector):
                if "captcha" in selector:
                    return ChallengeLocator()
                return ControlsLocator()

        result = self.adapter.analyze_page(Page())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["fields"][0]["visible"], True)
        self.assertEqual(result["fields"][0]["editable"], True)
        self.assertEqual(result["fields"][0]["interactive"], True)

    @patch("services.applications.adapters.workable.analyze_form_page")
    def test_inert_ancestor_makes_contact_control_require_review(self, analyzer):
        analyzer.return_value = {"status": "PASS", "fields": [{
            "index": 0, "tag": "input", "input_type": "email", "name": "email",
            "id": "email", "labels": ["Email"], "sensitivity": "STANDARD",
            "ambiguous": False,
        }]}

        class ChallengeLocator:
            def count(self):
                return 0

        class ControlsLocator:
            def evaluate_all(self, script):
                self.script = script
                return [{
                    "tag": "input", "input_type": "email", "name": "email", "id": "email",
                    "visible": True, "editable": False, "interactive": False,
                }]

        class Page:
            def __init__(self):
                self.controls = ControlsLocator()

            def locator(self, selector):
                if "captcha" in selector:
                    return ChallengeLocator()
                return self.controls

        page = Page()
        result = self.adapter.analyze_page(page)
        self.assertIn("closest('[inert]')", page.controls.script)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["fields"][0]["editable"])
        self.assertFalse(result["fields"][0]["interactive"])
        self.assertTrue(self.adapter.requires_human_review(result))

    @patch("services.applications.adapters.workable.analyze_form_page")
    def test_analyze_page_fails_closed_when_interactivity_cannot_be_verified(self, analyzer):
        analyzer.return_value = {"status": "PASS", "fields": [{
            "index": 0, "tag": "input", "input_type": "email", "name": "email",
            "id": "email", "labels": ["Email"], "sensitivity": "STANDARD",
            "ambiguous": False,
        }]}

        class ChallengeLocator:
            def count(self):
                return 0

        class FailedControlsLocator:
            def evaluate_all(self, script, **kwargs):
                raise RuntimeError("synthetic read failure")

        class Page:
            def locator(self, selector):
                if "captcha" in selector:
                    return ChallengeLocator()
                return FailedControlsLocator()

        self.assertEqual(self.adapter.analyze_page(Page()), {
            "status": "UNKNOWN", "reason": "CONTROL_INTERACTIVITY_UNAVAILABLE", "fields": [],
        })

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
