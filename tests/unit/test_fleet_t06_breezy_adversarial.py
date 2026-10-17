"""Adversarial static-projection checks for the T06 Breezy worker contract."""
import unittest

from services.applications.adapters.breezy import BreezyApplicationAdapter


ROUTE = "https://acme.breezy.hr/p/389eb1b8c3ef-job"


class FleetT06BreezyAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BreezyApplicationAdapter()

    def analyze(self, html):
        return self.adapter.analyze_form(html, route_url=ROUTE)

    def assert_fixed_review(self, report, reason):
        self.assertEqual(report, {
            "status": "REVIEW_REQUIRED", "reason": reason, "fields": [],
        })

    def test_challenge_markers_in_inert_markup_return_only_fixed_review_code(self):
        specimens = (
            '<form><div data-cf-chl="PRIVATE_CHALLENGE_TOKEN">'
            '<input name="email" value="PRIVATE_ANSWER"></div></form>',
            '<form><script>turnstile PRIVATE_SCRIPT_CHALLENGE</script>'
            '<input name="email" value="PRIVATE_ANSWER"></form>',
        )
        for markup in specimens:
            with self.subTest(markup=markup[:40]):
                report = self.analyze(markup)
                self.assert_fixed_review(report, "CHALLENGE_PROTECTED")
                for private_text in (
                        "PRIVATE_CHALLENGE_TOKEN", "PRIVATE_SCRIPT_CHALLENGE",
                        "PRIVATE_ANSWER", "turnstile", "cf-chl"):
                    self.assertNotIn(private_text, repr(report))

    def test_duplicate_attributes_multiple_forms_and_outside_controls_fail_closed(self):
        specimens = (
            ('duplicate-attribute',
             '<form><input name="email" name="PRIVATE_ATTRIBUTE" '
             'value="PRIVATE_VALUE"></form>', "MARKUP_PARSE_FAILED"),
            ('multiple-forms',
             '<form><input name="email" value="PRIVATE_VALUE"></form>'
             '<form><input name="name"></form>', "AMBIGUOUS_FORM"),
            ('outside-control',
             '<input name="outside" value="PRIVATE_VALUE">'
             '<form><input name="email"></form>', "AMBIGUOUS_FORM"),
        )
        for name, markup, reason in specimens:
            with self.subTest(case=name):
                report = self.analyze(markup)
                self.assert_fixed_review(report, reason)
                self.assertNotIn("PRIVATE", repr(report))

    def test_duplicate_control_identity_keeps_both_fields_ambiguous_and_redacted(self):
        report = self.analyze(
            '<form><input id="same-control" name="email" type="email" '
            'value="PRIVATE_FIRST">'
            '<input id="same-control" name="backup" type="text" '
            'value="PRIVATE_SECOND"></form>'
        )

        self.assert_fixed_review(report, "AMBIGUOUS_CONTROL_IDENTITY")
        serialized = repr(report)
        for private_text in ("same-control", "PRIVATE_FIRST", "PRIVATE_SECOND"):
            self.assertNotIn(private_text, serialized)

    def test_unicode_identifier_controls_cannot_smuggle_metadata(self):
        report = self.analyze(
            '<form><input id="email\u202e" name="email\u200b" type="email" '
            'aria-label="PRIVATE_ACCESS_PROMPT" value="PRIVATE_VALUE"></form>'
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual(report["fields"], [{
            "id": None, "name": None, "type": "email", "required": False,
            "review_reason": "UNSAFE_IDENTIFIER",
        }])
        for private_text in ("PRIVATE_ACCESS_PROMPT", "PRIVATE_VALUE", "\u202e", "\u200b"):
            self.assertNotIn(private_text, repr(report))

    def test_utf8_byte_and_markup_token_budgets_fail_closed(self):
        # The Python string is below the character cap but exceeds the UTF-8
        # byte cap because this payload uses two-byte code points.
        multibyte = "<form>" + ("é" * 50_000) + "</form>"
        self.assertLess(len(multibyte), 100_000)
        self.assertGreater(len(multibyte.encode("utf-8")), 100_000)
        byte_limited = self.analyze(multibyte)
        self.assert_fixed_review(byte_limited, "MARKUP_LIMIT_EXCEEDED")

        tag_storm = "<form>" + ("<div></div>" * 2_050) + "</form>"
        self.assertLess(len(tag_storm.encode("utf-8")), 100_000)
        token_limited = self.analyze(tag_storm)
        self.assert_fixed_review(token_limited, "MARKUP_LIMIT_EXCEEDED")

    def test_credential_and_sensitive_controls_remain_flagged_with_only_safe_fields(self):
        report = self.analyze(
            '<form><input id="auth" name="sign_in" type="PASSWORD" '
            'value="PRIVATE_CREDENTIAL">'
            '<label for="email">Social Security number</label>'
            '<input id="email" name="email" type="text" '
            'value="PRIVATE_SENSITIVE"></form>'
        )

        self.assertEqual(report["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["reason"], "MANUAL_REVIEW_REQUIRED")
        self.assertEqual([field["review_reason"] for field in report["fields"]],
                         ["CREDENTIAL_FIELD", "SENSITIVE_FIELD"])
        serialized = repr(report)
        for private_text in ("PRIVATE_CREDENTIAL", "PRIVATE_SENSITIVE", "Social Security"):
            self.assertNotIn(private_text, serialized)


if __name__ == "__main__":
    unittest.main()
