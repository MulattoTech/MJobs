import unittest

from services.applications.adapters.breezy import BreezyApplicationAdapter
from services.applications.breezy_projection import analyze_breezy_form_html


class BreezyProjectionTests(unittest.TestCase):
    route = "https://acme.breezy.hr/p/389eb1b8c3ef-software-engineer"

    def test_projects_safe_fields_and_always_requires_manual_review(self):
        report = analyze_breezy_form_html(
            b'<form><label for="email">Email address</label>'
            b'<input id="email" name="email" type="email" required '
            b'value="private@example.test"></form>',
            route_url=self.route,
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(report["fields"], [{
            "id": "email", "name": "email", "type": "email", "required": True,
            "review_reason": "UNKNOWN_QUESTION",
        }])
        self.assertTrue(BreezyApplicationAdapter.requires_human_review(report))
        self.assertNotIn("private@example.test", repr(report))
        self.assertNotIn("Email address", repr(report))

    def test_rejects_unsupported_route_with_fixed_report(self):
        report = analyze_breezy_form_html(
            '<form><input name="email" value="secret-answer"></form>',
            route_url="https://example.test/apply",
        )

        self.assertEqual(report, {
            "status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": [],
        })

    def test_custom_prompt_identifiers_types_and_options_are_not_returned(self):
        markup = (
            '<form>'
            '<label for="custom-id">Why do you want this job? confidential-prompt</label>'
            '<input id="Why do you want this job? private-id" '
            'name="custom applicant question private-name" '
            'type="Why do you want this job? private-type" '
            'placeholder="private-placeholder" aria-label="private-aria" '
            'value="private-value">'
            '<select id="country"><option>private-option-answer</option></select>'
            '</form>'
        )
        report = analyze_breezy_form_html(markup, route_url=self.route)
        serialized = repr(report)

        for private_text in (
            "confidential-prompt", "private-id", "private-name", "private-type",
            "private-placeholder", "private-aria", "private-value", "private-option-answer",
            "Why do you want this job?",
        ):
            with self.subTest(private_text=private_text):
                self.assertNotIn(private_text, serialized)
        reasons = {field["id"]: field["review_reason"] for field in report["fields"]}
        self.assertEqual(reasons[None], "UNSAFE_IDENTIFIER")
        self.assertEqual(reasons["country"], "UNKNOWN_QUESTION")

    def test_preserves_known_sensitive_and_credential_review_reasons(self):
        report = analyze_breezy_form_html(
            '<form><input id="email" name="password" type="password">'
            '<label for="ssn">Social Security number</label>'
            '<input id="ssn" name="ssn" type="text"></form>',
            route_url=self.route,
        )

        self.assertEqual(report["reason"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(
            [field["review_reason"] for field in report["fields"]],
            ["CREDENTIAL_FIELD", "SENSITIVE_FIELD"],
        )

    def test_invalid_utf8_size_and_control_count_fail_closed(self):
        invalid_utf8 = analyze_breezy_form_html(b"\xff", route_url=self.route)
        oversized = analyze_breezy_form_html("x" * 100_001, route_url=self.route)
        excessive_controls = analyze_breezy_form_html(
            "<form>" + "<input name='email'>" * 101 + "</form>", route_url=self.route,
        )

        self.assertEqual(invalid_utf8["reason"], "INVALID_FORM_HTML")
        self.assertEqual(oversized["reason"], "MARKUP_LIMIT_EXCEEDED")
        self.assertEqual(excessive_controls["reason"], "CONTROL_LIMIT_EXCEEDED")
        self.assertEqual(invalid_utf8["fields"], [])
        self.assertEqual(oversized["fields"], [])
        self.assertEqual(excessive_controls["fields"], [])


if __name__ == "__main__":
    unittest.main()
