import unittest

from services.applications.breezy_projection import analyze_breezy_form_html


class BreezyProjectionTests(unittest.TestCase):
    route = "https://acme.breezy.hr/p/123e4567-e89b-12d3-a456-426614174000-software-engineer"

    def test_projects_bounded_safe_fields_and_always_requires_manual_review(self):
        report = analyze_breezy_form_html(
            '<form><label for="email">Email address</label>'
            '<input id="email" name="email" type="email" required '
            'value="private@example.test"></form>',
            route_url=self.route,
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "STATIC_VISIBILITY_UNVERIFIED")
        self.assertEqual(report["fields"], [{
            "id": "email", "name": "email", "type": "email", "required": True,
            "review_reason": "STATIC_VISIBILITY_UNVERIFIED",
        }])
        self.assertNotIn("private@example.test", repr(report))
        self.assertNotIn("Email address", repr(report))

    def test_rejects_wrong_route_without_exposing_markup(self):
        report = analyze_breezy_form_html(
            '<form><input name="Tell us why you want this job" '
            'value="secret-answer"></form>',
            route_url="https://example.test/apply",
        )

        self.assertEqual(report, {
            "status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": [],
        })

    def test_invalid_or_ambiguous_markup_fails_closed(self):
        for markup, reason in (
            ("<p>No form</p>", "NO_FORM"),
            ("<form><input name='email'></form><form><input name='name'></form>", "AMBIGUOUS_FORM"),
            ("<input name='outside'><form><input name='email'></form>", "AMBIGUOUS_FORM"),
            ("<form><input id='email'><input id='email'></form>", "AMBIGUOUS_CONTROL_IDENTITY"),
            ("<form><input name='email' name='name'></form>", "MARKUP_PARSE_FAILED"),
        ):
            with self.subTest(reason=reason, markup=markup):
                report = analyze_breezy_form_html(markup, route_url=self.route)
                self.assertEqual(report["status"], "REVIEW_REQUIRED")
                self.assertEqual(report["reason"], reason)
                self.assertEqual(report["fields"], [])

    def test_size_limit_and_anti_bot_marker_fail_closed(self):
        oversized = analyze_breezy_form_html("x" * 100_001, route_url=self.route)
        challenged = analyze_breezy_form_html(
            '<form><div class="cf-turnstile"></div><input name="email"></form>',
            route_url=self.route,
        )

        self.assertEqual(oversized["reason"], "MARKUP_LIMIT_EXCEEDED")
        self.assertEqual(challenged["reason"], "CHALLENGE_PROTECTED")
        self.assertEqual(oversized["fields"], [])
        self.assertEqual(challenged["fields"], [])


if __name__ == "__main__":
    unittest.main()
