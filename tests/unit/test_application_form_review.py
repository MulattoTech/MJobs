"""Sanitized Greenhouse form HTML analysis tests."""
import json
import unittest

from services.applications.form_review import analyze_form_html


class ApplicationFormReviewTests(unittest.TestCase):
    route_url = "https://boards.greenhouse.io/example/jobs/1234"

    def test_returns_only_bounded_value_free_field_metadata(self):
        html = (
            '<form><label for="email">Work email</label>'
            '<input id="email" name="email" type="email" required value="PRIVATE_VALUE">'
            '<label for="note">Optional note</label>'
            '<textarea id="note" name="note">PRIVATE_ANSWER</textarea>'
            '<select id="region" name="region"><option value="private">Canada</option></select>'
            '</form>'
        )
        report = analyze_form_html("greenhouse", html, route_url=self.route_url)

        self.assertEqual(report["status"], "ANALYZED")
        self.assertIsNone(report["reason"])
        self.assertEqual(report["fields"], [
            {"id": "email", "name": "email", "type": "email", "required": True,
             "review_reason": None},
            {"id": "note", "name": "note", "type": "textarea", "required": False,
             "review_reason": None},
            {"id": "region", "name": "region", "type": "select", "required": False,
             "review_reason": None},
        ])
        serialized = json.dumps(report)
        for private_text in ("PRIVATE_VALUE", "PRIVATE_ANSWER", "Work email", "Canada", "private"):
            self.assertNotIn(private_text, serialized)
        self.assertTrue(all(set(field) == {"id", "name", "type", "required", "review_reason"}
                            for field in report["fields"]))

    def test_sensitive_credentials_ambiguous_and_challenge_forms_require_review(self):
        cases = (
            ('<form><input id="ssn" name="ssn" type="text"></form>',
             "FORM_REQUIRES_HUMAN_REVIEW", "SENSITIVE_FIELD"),
            ('<form><input id="account" name="password" type="password"></form>',
             "FORM_REQUIRES_HUMAN_REVIEW", "CREDENTIAL_FIELD"),
            ('<form><input id="same" name="email"><input id="same" name="other"></form>',
             "AMBIGUOUS_CONTROL_IDENTITY", "AMBIGUOUS_FIELD"),
            ('<form><input name="email"></form><form><input name="other"></form>',
             "AMBIGUOUS_FORM", "AMBIGUOUS_FORM"),
            ('<form><input name="email"></form><div class="h-captcha"></div>',
             "CHALLENGE_PROTECTED", "CHALLENGE_PROTECTED"),
        )
        for html, expected_reason, field_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                report = analyze_form_html("greenhouse", html, route_url=self.route_url)
                self.assertEqual(report["status"], "REVIEW_REQUIRED")
                self.assertEqual(report["reason"], expected_reason)
                if expected_reason == "CHALLENGE_PROTECTED":
                    self.assertEqual(report["fields"], [])
                else:
                    self.assertTrue(all(field["review_reason"] == field_reason for field in report["fields"]))

    def test_rejects_unsupported_route_and_bounds_html_without_echoing_input(self):
        invalid_route = analyze_form_html(
            "greenhouse", "<form><input name='email'></form>",
            route_url="https://boards.greenhouse.io.attacker.test/example/jobs/1234",
        )
        self.assertEqual(invalid_route, {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []})

        too_large = analyze_form_html("greenhouse", "x" * (2 * 1024 * 1024 + 1), route_url=self.route_url)
        self.assertEqual(too_large, {"status": "REVIEW_REQUIRED", "reason": "HTML_TOO_LARGE", "fields": []})
        self.assertNotIn("x" * 100, json.dumps(too_large))

    def test_missing_or_unidentifiable_forms_require_review(self):
        no_form = analyze_form_html("greenhouse", "<p>No form</p>", route_url=self.route_url)
        no_fields = analyze_form_html("greenhouse", "<form><input type='email'></form>", route_url=self.route_url)
        self.assertEqual(no_form["reason"], "NO_FORM")
        self.assertEqual(no_fields["reason"], "UNIDENTIFIABLE_CONTROL")
        self.assertEqual(no_form["status"], "REVIEW_REQUIRED")
        self.assertEqual(no_fields["status"], "REVIEW_REQUIRED")

    def test_personal_data_shaped_identifiers_are_not_returned(self):
        report = analyze_form_html(
            "greenhouse",
            '<form><input name="dylan@example.com" type="text"></form>',
            route_url=self.route_url,
        )
        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "FORM_REQUIRES_HUMAN_REVIEW")
        self.assertIsNone(report["fields"][0]["name"])
        self.assertEqual(report["fields"][0]["review_reason"], "UNSAFE_IDENTIFIER")
        self.assertNotIn("dylan@example.com", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
