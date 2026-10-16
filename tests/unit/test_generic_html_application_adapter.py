"""Synthetic contract tests for the generic HTML safe baseline."""
import json
import unittest

from services.applications.adapters.generic_html import GenericHTMLApplicationAdapter


class GenericHTMLApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = GenericHTMLApplicationAdapter()

    def test_capabilities_are_explicit_and_all_actions_remain_disabled(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "bounded_control_recognition": True,
            "human_review_for_unknown_fields": True,
            "form_metadata_extraction": False,
            "autofill": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_normalizes_https_route_without_fetching(self):
        self.assertEqual(self.adapter.normalize_route({
            "provider": "generic_html",
            "url": "https://Careers.Example/jobs/42?source=fixture",
        }), {
            "provider": "generic_html",
            "tenant": "careers.example",
            "url": "https://careers.example/jobs/42?source=fixture",
        })

    def test_rejects_ambiguous_or_unsafe_routes(self):
        for route in (
            None,
            {"provider": "greenhouse", "url": "https://careers.example/jobs/42"},
            {"provider": "generic_html", "url": "http://careers.example/jobs/42"},
            {"provider": "generic_html", "url": "https://user@careers.example/jobs/42"},
            {"provider": "generic_html", "url": "https://careers.example:8443/jobs/42"},
            {"provider": "generic_html", "url": "https://careers.example/jobs%2fother"},
            {"provider": "generic_html", "url": "https://careers.example/jobs/../admin"},
            {"provider": "generic_html", "url": "https://careers.example/jobs/./apply"},
            {"provider": "generic_html", "url": r"https://careers.example/jobs\..\admin"},
            {"provider": "generic_html", "url": "https://careers.example/jobs/42#apply"},
            {"provider": "generic_html", "url": "https://careers.example/jobs/42",
             "host": "attacker.example"},
        ):
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_recognizes_bounded_metadata_without_exposing_answers(self):
        report = self.adapter.recognize_controls([{
            "tag": "input", "input_type": "email", "name": "email",
            "label": "Email address",
        }])
        self.assertEqual(report["status"], "PASS")
        self.assertNotIn("value", json.dumps(report).casefold())
        self.assertNotIn("applicant@example.test", json.dumps(report))
        self.assertTrue(self.adapter.requires_human_review(report))

    def test_unknown_sensitive_ambiguous_and_malformed_controls_fail_closed(self):
        unknown = self.adapter.recognize_controls([{
            "tag": "textarea", "label": "Why do you want this role?",
            "name": "motivation",
        }])
        sensitive = self.adapter.recognize_controls([{
            "tag": "input", "input_type": "text", "label": "Social Security number",
        }])
        malformed = self.adapter.recognize_controls([{"tag": "button"}])
        oversized = self.adapter.recognize_controls([{
            "tag": "input", "name": f"field_{index}", "label": f"Field {index}",
        } for index in range(101)])
        for report in (unknown, sensitive, malformed, oversized,
                       {"status": "UNKNOWN", "fields": []}, None):
            with self.subTest(report=report):
                self.assertTrue(self.adapter.requires_human_review(report))
        self.assertEqual(oversized["reason"], "CONTROL_LIMIT_EXCEEDED")
        self.assertEqual(malformed["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
