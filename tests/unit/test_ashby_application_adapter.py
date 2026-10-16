"""Synthetic contract tests for the review-first Ashby application adapter."""
import json
import unittest

from services.applications.adapters.ashby import AshbyApplicationAdapter


class AshbyApplicationAdapterTests(unittest.TestCase):
    route = "https://jobs.ashbyhq.com/example/engineering-123"

    def setUp(self):
        self.adapter = AshbyApplicationAdapter()

    def test_declares_only_route_and_value_free_review_capabilities(self):
        self.assertEqual(self.adapter.capabilities(), {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        })

    def test_normalizes_strict_ashby_job_route_and_checks_supplied_identity(self):
        normalized = self.adapter.normalize_route({
            "provider": "ASHBY",
            "url": "https://JOBS.ASHBYHQ.COM/Example/Engineering-123?source=fixture",
            "tenant": "example",
            "requisition": "engineering-123",
        })
        ascii_case_variant = self.adapter.normalize_route({
            "provider": "ashby",
            "url": self.route,
            "tenant": "EXAMPLE",
            "requisition": "ENGINEERING-123",
        })

        self.assertEqual(normalized, {
            "provider": "ashby",
            "tenant": "example",
            "requisition": "engineering-123",
            "url": self.route,
        })
        self.assertEqual(ascii_case_variant, normalized)

    def test_rejects_unicode_casefold_aliases_in_supplied_route_metadata(self):
        invalid_metadata = [
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/k/job", "tenant": "K"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/longs/job", "tenant": "longſ"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/k", "requisition": "K"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/longs", "requisition": "longſ"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/i/job", "tenant": "İ"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/i/job", "tenant": "ı"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/i", "requisition": "İ"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/i", "requisition": "ı"},
        ]

        for route in invalid_metadata:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_rejects_unsafe_origins_route_shapes_and_mismatched_identity(self):
        invalid = [
            {"provider": "lever", "url": self.route},
            {"provider": "ashby", "url": "http://jobs.ashbyhq.com/example/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com.attacker.test/example/job"},
            {"provider": "ashby", "url": "https://user@jobs.ashbyhq.com/example/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com:443/example/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/job/extra"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/job#fragment"},
            {"provider": "ashby", "url": self.route, "tenant": "other"},
            {"provider": "ashby", "url": self.route, "requisition": "other"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/%2f/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/Kelvin/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/longſ/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/dottedİ/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/dotlessı/job"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/kelvinK"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/longſ"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/dottedİ"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/example/dotlessı"},
            {"provider": "ashby", "url": "https://jobs.ashbyhq.com/" + "a" * 2050},
            None,
        ]

        for route in invalid:
            with self.subTest(route=route):
                self.assertIsNone(self.adapter.normalize_route(route))

    def test_analyzes_known_static_fields_without_returning_values_or_prompt_copy(self):
        markup = (
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" required value="PRIVATE_VALUE">'
            '</form>'
        )

        report = self.adapter.analyze_form(markup, route_url=self.route)

        self.assertEqual(report, {
            "status": "ANALYZED",
            "reason": None,
            "fields": [{
                "id": "email", "name": "email", "type": "email",
                "required": True, "review_reason": None,
            }],
        })
        self.assertNotIn("PRIVATE_VALUE", json.dumps(report))
        self.assertNotIn("Email address", json.dumps(report))

    def test_unknown_sensitive_challenge_and_malformed_forms_fail_closed(self):
        cases = [
            ('<form><label for="q">Describe your work authorization</label>'
             '<textarea id="q" name="q">PRIVATE_ANSWER</textarea></form>',
             "FORM_REQUIRES_HUMAN_REVIEW"),
            ('<form><input id="email" name="email" type="email">'
             '<input id="ssn" name="ssn"></form>', "FORM_REQUIRES_HUMAN_REVIEW"),
            ('<form><div class="cf-turnstile">PRIVATE_CHALLENGE</div>'
             '<input id="email" name="email" type="email"></form>', "CHALLENGE_PROTECTED"),
            ('<input id="email" name="email" type="email">', "NO_FORM"),
        ]

        for markup, reason in cases:
            with self.subTest(reason=reason):
                report = self.adapter.analyze_form(markup, route_url=self.route)
                self.assertEqual(report["status"], "REVIEW_REQUIRED")
                self.assertEqual(report["reason"], reason)
                for secret in ("PRIVATE_ANSWER", "PRIVATE_CHALLENGE", "work authorization"):
                    self.assertNotIn(secret, json.dumps(report))

    def test_unsupported_route_and_invalid_markup_return_fixed_value_free_reports(self):
        self.assertEqual(self.adapter.analyze_form(
            "<form></form>", route_url="https://jobs.ashbyhq.com.attacker.test/example/job",
        ), {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []})
        self.assertEqual(self.adapter.analyze_form(
            object(), route_url=self.route,
        ), {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []})


if __name__ == "__main__":
    unittest.main()
