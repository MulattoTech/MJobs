import unittest

from services.documents.sensitive_answer_policy import (
    ALWAYS_REQUIRE_REVIEW,
    PROFILE_POLICY_SOURCE,
    classify_sensitive_category,
    resolve_sensitive_answer,
)


class SensitiveAnswerPolicyTests(unittest.TestCase):
    def test_classifies_only_the_bounded_sensitive_categories(self):
        cases = {
            "What is your race or ethnicity?": "race_ethnicity",
            "Please state your religious affiliation.": "religion",
            "What is your gender identity?": "sex_gender",
            "Do you have a disability?": "disability",
            "Are you a veteran?": "veteran_status",
            "What is your date of birth?": "other_voluntary_demographics",
            "Are you legally authorized to work here?": None,
            "Tell us about your experience with Python.": None,
        }

        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(classify_sensitive_category(question), expected)

    def test_missing_invalid_and_unknown_policy_fail_closed(self):
        cases = (
            ("race_ethnicity", None),
            ("race_ethnicity", []),
            ("race_ethnicity", {"race_ethnicity": {"mode": "GUESS"}}),
            ("unrecognized", {"unrecognized": {
                "mode": "USE_CONFIGURED_RESPONSE", "response": "invented"}}),
        )

        for category, policies in cases:
            with self.subTest(category=category, policies=policies):
                result = resolve_sensitive_answer(category, policies)
                self.assertEqual(result["response"], "")
                self.assertEqual(result["mode"], ALWAYS_REQUIRE_REVIEW)
                self.assertTrue(result["review_required"])

    def test_only_explicit_prefer_not_to_answer_returns_the_literal_response(self):
        result = resolve_sensitive_answer("religion", {
            "religion": {"mode": "PREFER_NOT_TO_ANSWER"},
        })

        self.assertEqual(result["response"], "Prefer not to answer")
        self.assertEqual(result["mode"], "PREFER_NOT_TO_ANSWER")
        self.assertEqual(result["source"], PROFILE_POLICY_SOURCE)
        self.assertTrue(result["review_required"])

    def test_always_require_review_never_returns_configured_text(self):
        result = resolve_sensitive_answer("veteran_status", {
            "veteran_status": {"mode": ALWAYS_REQUIRE_REVIEW},
        })

        self.assertEqual(result["response"], "")
        self.assertEqual(result["mode"], ALWAYS_REQUIRE_REVIEW)
        self.assertTrue(result["review_required"])

    def test_configured_response_is_returned_verbatim_and_requires_review(self):
        configured = "  I prefer to self-describe: Example  "
        result = resolve_sensitive_answer("other_voluntary_demographics", {
            "other_voluntary_demographics": {
                "mode": "USE_CONFIGURED_RESPONSE",
                "response": configured,
            },
        })

        self.assertEqual(result["response"], configured)
        self.assertEqual(result["mode"], "USE_CONFIGURED_RESPONSE")
        self.assertEqual(result["source"], PROFILE_POLICY_SOURCE)
        self.assertTrue(result["review_required"])

    def test_invalid_configured_response_never_becomes_an_answer(self):
        for response in ("", " \t ", None, 42, "x" * 2001):
            with self.subTest(response=repr(response)):
                result = resolve_sensitive_answer("disability", {
                    "disability": {
                        "mode": "USE_CONFIGURED_RESPONSE",
                        "response": response,
                    },
                })
                self.assertEqual(result["response"], "")
                self.assertEqual(result["mode"], ALWAYS_REQUIRE_REVIEW)
                self.assertTrue(result["review_required"])

    def test_question_with_no_recognized_category_never_uses_any_policy(self):
        result = resolve_sensitive_answer(None, {
            "race_ethnicity": {
                "mode": "USE_CONFIGURED_RESPONSE",
                "response": "Example",
            },
        })

        self.assertEqual(result, {
            "response": "",
            "mode": ALWAYS_REQUIRE_REVIEW,
            "review_required": True,
        })


if __name__ == "__main__":
    unittest.main()
