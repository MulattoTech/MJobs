"""Catch authorization, truthfulness, limit and retry gate regressions."""
from dataclasses import replace
import unittest

from packages.domain.safety import (
    Fact, Policy, SubmissionContext, evaluate_submission, select_verified_facts,
)


class SubmissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.context = SubmissionContext(
            target="MOCK", state="READY_TO_SUBMIT", match_score=90,
            confidence=95, quality_approved=True, facts_verified=True,
            source_allowed=True, sensitive_answers_approved=True,
        )

    def test_local_mock_with_all_gates_can_submit(self):
        self.assertTrue(evaluate_submission(self.context, Policy()).allowed)

    def test_external_is_disabled_by_default(self):
        result = evaluate_submission(replace(self.context, target="EXTERNAL"), Policy())
        self.assertFalse(result.allowed)
        self.assertIn("EXTERNAL_SUBMISSION_DISABLED", result.reasons)

    def test_policy_enablement_does_not_replace_explicit_user_enablement(self):
        result = evaluate_submission(replace(self.context, target="EXTERNAL"),
                                     Policy(external_submissions_enabled=True))
        self.assertFalse(result.allowed)
        self.assertIn("USER_ENABLEMENT_REQUIRED", result.reasons)

    def test_enablement_does_not_override_any_quality_gate(self):
        base = replace(self.context, target="EXTERNAL", user_enabled=True)
        cases = [
            ({"match_score": 79}, "MATCH_BELOW_THRESHOLD"),
            ({"confidence": 79}, "CONFIDENCE_BELOW_THRESHOLD"),
            ({"quality_approved": False}, "QUALITY_REVIEW_REQUIRED"),
            ({"facts_verified": False}, "UNVERIFIED_FACTS"),
            ({"source_allowed": False}, "SOURCE_POLICY_BLOCKED"),
            ({"sensitive_answers_approved": False}, "SENSITIVE_REVIEW_REQUIRED"),
            ({"duplicate": True}, "DUPLICATE_APPLICATION"),
            ({"hourly_count": 3}, "HOURLY_LIMIT_REACHED"),
            ({"daily_count": 10}, "DAILY_LIMIT_REACHED"),
            ({"company_count": 1}, "COMPANY_LIMIT_REACHED"),
            ({"state": "PREPARING"}, "INVALID_SUBMISSION_STATE"),
        ]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                result = evaluate_submission(replace(base, **changes),
                                             Policy(external_submissions_enabled=True))
                self.assertFalse(result.allowed)
                self.assertIn(reason, result.reasons)

    def test_challenges_always_require_intervention(self):
        for barrier in ("CAPTCHA", "MFA", "ACCESS_CONTROL", "ANTI_BOT", "RATE_LIMIT"):
            with self.subTest(barrier=barrier):
                result = evaluate_submission(replace(self.context, barrier=barrier), Policy())
                self.assertFalse(result.allowed)
                self.assertIn("USER_INTERVENTION_REQUIRED", result.reasons)

    def test_ambiguous_submission_never_blindly_retries(self):
        result = evaluate_submission(replace(self.context, state="SUBMISSION_UNCONFIRMED"), Policy())
        self.assertFalse(result.allowed)
        self.assertIn("SUBMISSION_UNCONFIRMED", result.reasons)

    def test_confirmed_submission_does_not_retry(self):
        self.assertFalse(evaluate_submission(replace(self.context, state="CONFIRMED"), Policy()).allowed)

    def test_threshold_boundary_is_inclusive(self):
        self.assertTrue(evaluate_submission(replace(self.context, match_score=80, confidence=80), Policy()).allowed)

    def test_invalid_inputs_fail_closed(self):
        for changes in ({"match_score": float("nan")}, {"confidence": float("inf")},
                        {"hourly_count": -1}, {"daily_count": True},
                        {"user_enabled": "false"}, {"target": "UNKNOWN"},
                        {"barrier": "UNKNOWN"}, {"quality_approved": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.context, **changes)

    def test_invalid_policy_cannot_remove_limits(self):
        for changes in ({"daily_limit": -1}, {"hourly_limit": True},
                        {"minimum_match": float("nan")},
                        {"external_submissions_enabled": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Policy(**changes)


class FactTests(unittest.TestCase):
    def setUp(self):
        self.fact = Fact(id="skill-python", text="Python", source="user-profile",
                         verified=True, user_approved=True, confidence=100)

    def test_only_existing_verified_approved_facts_are_selected(self):
        self.assertEqual(select_verified_facts([self.fact], ["skill-python"]), (self.fact,))

    def test_unknown_requirement_does_not_invent_a_fact(self):
        with self.assertRaises(ValueError):
            select_verified_facts([self.fact], ["skill-aws-lambda"])

    def test_unverified_unapproved_or_unproven_facts_are_rejected(self):
        for changes in ({"verified": False}, {"user_approved": False},
                        {"source": ""}, {"confidence": 79}, {"text": " "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                select_verified_facts([replace(self.fact, **changes)], ["skill-python"])

    def test_duplicate_fact_identity_is_ambiguous(self):
        with self.assertRaises(ValueError):
            select_verified_facts([self.fact, replace(self.fact, text="AWS")], ["skill-python"])

    def test_duplicate_selection_does_not_repeat_facts(self):
        self.assertEqual(select_verified_facts([self.fact], ["skill-python", "skill-python"]), (self.fact,))

    def test_strings_are_not_boolean_verification(self):
        with self.assertRaises(ValueError):
            replace(self.fact, verified="true")


if __name__ == "__main__":
    unittest.main()
