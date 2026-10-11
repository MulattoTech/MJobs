import json
import unittest

from services.applications.form_review_providers import (
    analyze_application_form,
    normalize_application_route,
)


class ApplicationFormReviewProvidersTests(unittest.TestCase):
    greenhouse_route = "https://job-boards.greenhouse.io/example/jobs/1234"
    lever_route = "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000"

    def test_normalizes_supported_routes_with_existing_adapters(self):
        self.assertEqual(normalize_application_route("GREENHOUSE", self.greenhouse_route), {
            "provider": "greenhouse",
            "tenant": "example",
            "requisition": "1234",
            "url": "https://boards.greenhouse.io/example/jobs/1234",
        })
        self.assertEqual(normalize_application_route("lever", self.lever_route), {
            "provider": "lever",
            "tenant": "example",
            "requisition": "123e4567-e89b-12d3-a456-426614174000",
            "url": self.lever_route,
        })

    def test_analyzes_greenhouse_html_without_returning_values_or_page_text(self):
        html = (
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" required value="PRIVATE_VALUE">'
            '</form>'
        )

        report = analyze_application_form("greenhouse", html, route_url=self.greenhouse_route)

        self.assertEqual(report, {
            "status": "ANALYZED", "reason": None,
            "fields": [{"id": "email", "name": "email", "type": "email",
                        "required": True, "review_reason": None}],
        })
        self.assertNotIn("PRIVATE_VALUE", json.dumps(report))
        self.assertNotIn("Email address", json.dumps(report))

    def test_analyzes_lever_markup_into_the_same_value_free_report_shape(self):
        markup = (
            '<form><label for="name">Full name</label>'
            '<input id="name" name="full_name" type="text" value="PRIVATE_VALUE">'
            '</form>'
        )

        report = analyze_application_form("lever", markup, route_url=self.lever_route)

        self.assertEqual(report, {
            "status": "ANALYZED", "reason": None,
            "fields": [{"id": "name", "name": "full_name", "type": "text",
                        "required": False, "review_reason": None}],
        })
        self.assertNotIn("PRIVATE_VALUE", json.dumps(report))
        self.assertNotIn("Full name", json.dumps(report))

    def test_sensitive_fields_require_human_review_for_both_providers(self):
        greenhouse = analyze_application_form(
            "greenhouse", '<form><input id="ssn" name="ssn"></form>',
            route_url=self.greenhouse_route,
        )
        lever = analyze_application_form(
            "lever", '<form><label for="birth">Date of birth</label>'
            '<input id="birth" name="birth_date"></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(greenhouse["status"], "REVIEW_REQUIRED")
        self.assertEqual(greenhouse["fields"][0]["review_reason"], "SENSITIVE_FIELD")
        self.assertEqual(lever["status"], "REVIEW_REQUIRED")
        self.assertEqual(lever["reason"], "FORM_REQUIRES_HUMAN_REVIEW")
        self.assertEqual(lever["fields"][0]["review_reason"], "SENSITIVE_FIELD")
        self.assertNotIn("Date of birth", json.dumps(lever))

    def test_ambiguous_lever_controls_require_human_review(self):
        report = analyze_application_form(
            "lever", '<form><input id="same" name="email">'
            '<input id="same" name="other"></form>',
            route_url=self.lever_route,
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "AMBIGUOUS_CONTROL_IDENTITY")
        self.assertEqual([field["review_reason"] for field in report["fields"]],
                         ["AMBIGUOUS_FIELD", "AMBIGUOUS_FIELD"])

    def test_unsupported_providers_and_routes_return_fixed_codes(self):
        self.assertEqual(analyze_application_form(
            "unknown", "<form></form>", route_url=self.greenhouse_route,
        ), {"status": "REVIEW_REQUIRED", "reason": "PROVIDER_UNSUPPORTED", "fields": []})
        self.assertEqual(analyze_application_form(
            "lever", "<form></form>", route_url="https://jobs.lever.co.attacker.test/a/b",
        ), {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []})
        self.assertIsNone(normalize_application_route("ashby", self.lever_route))

    def test_route_normalization_rejects_credentials_ports_and_unknown_paths(self):
        invalid_routes = (
            ("greenhouse", "https://user@boards.greenhouse.io/example/jobs/1234"),
            ("greenhouse", "https://boards.greenhouse.io:8443/example/jobs/1234"),
            ("lever", "https://jobs.lever.co:443/example/123e4567-e89b-12d3-a456-426614174000"),
            ("lever", "https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000/unknown"),
        )
        for provider, route_url in invalid_routes:
            with self.subTest(provider=provider, route_url=route_url):
                self.assertIsNone(normalize_application_route(provider, route_url))


if __name__ == "__main__":
    unittest.main()
