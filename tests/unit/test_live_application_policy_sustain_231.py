"""MJ-REQ-230: real submission needs distinct, explicit user intent."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

from packages.domain.safety import Policy, SubmissionContext, evaluate_submission


class LiveApplicationPolicyTests(unittest.TestCase):
    def external_context(self, **changes):
        evaluated_at = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)
        context = SubmissionContext(
            target="EXTERNAL",
            state="READY_TO_SUBMIT",
            user_enabled=True,
            actual_user_intended_job=True,
            job_id="job-a",
            intent_receipt_id="intent-a",
            intent_job_id="job-a",
            intent_granted_at=evaluated_at - timedelta(minutes=5),
            intent_expires_at=evaluated_at + timedelta(minutes=5),
            intent_consumed=False,
            evaluated_at=evaluated_at,
            match_score=100,
            confidence=100,
            quality_approved=True,
            facts_verified=True,
            source_allowed=True,
            sensitive_answers_approved=True,
        )
        return replace(context, **changes)

    def test_external_enablement_cannot_stand_in_for_actual_job_intent(self):
        """Enabling submission is not proof that this specific job is intended."""
        decision = evaluate_submission(
            SubmissionContext(
                target="EXTERNAL",
                state="READY_TO_SUBMIT",
                user_enabled=True,
                match_score=100,
                confidence=100,
                quality_approved=True,
                facts_verified=True,
                source_allowed=True,
                sensitive_answers_approved=True,
            ),
            Policy(external_submissions_enabled=True),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("ACTUAL_USER_INTENT_REQUIRED", decision.reasons)

    def test_boolean_intent_without_durable_job_receipt_fails_closed(self):
        """A transient yes/no value is not durable consent for a specific job."""
        decision = evaluate_submission(
            self.external_context(intent_receipt_id=""),
            Policy(external_submissions_enabled=True),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("DURABLE_JOB_INTENT_REQUIRED", decision.reasons)

    def test_intent_for_another_job_cannot_authorize_submission(self):
        decision = evaluate_submission(
            self.external_context(job_id="job-b"),
            Policy(external_submissions_enabled=True),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("JOB_INTENT_MISMATCH", decision.reasons)

    def test_expired_job_intent_fails_closed(self):
        evaluated_at = self.external_context().evaluated_at
        decision = evaluate_submission(
            self.external_context(intent_expires_at=evaluated_at),
            Policy(external_submissions_enabled=True),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("STALE_JOB_INTENT", decision.reasons)

    def test_consumed_job_intent_cannot_be_replayed(self):
        decision = evaluate_submission(
            self.external_context(intent_consumed=True),
            Policy(external_submissions_enabled=True),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("JOB_INTENT_ALREADY_CONSUMED", decision.reasons)

    def test_live_submission_requires_every_independent_gate(self):
        enabled = Policy(external_submissions_enabled=True)
        self.assertTrue(evaluate_submission(self.external_context(), enabled).allowed)

        cases = (
            ({"actual_user_intended_job": False}, "ACTUAL_USER_INTENT_REQUIRED"),
            ({"user_enabled": False}, "USER_ENABLEMENT_REQUIRED"),
            ({"quality_approved": False}, "QUALITY_REVIEW_REQUIRED"),
            ({"facts_verified": False}, "UNVERIFIED_FACTS"),
            ({"sensitive_answers_approved": False}, "SENSITIVE_REVIEW_REQUIRED"),
            ({"source_allowed": False}, "SOURCE_POLICY_BLOCKED"),
            ({"match_score": 79}, "MATCH_BELOW_THRESHOLD"),
            ({"confidence": 79}, "CONFIDENCE_BELOW_THRESHOLD"),
            ({"duplicate": True}, "DUPLICATE_APPLICATION"),
            ({"hourly_count": 3}, "HOURLY_LIMIT_REACHED"),
            ({"daily_count": 10}, "DAILY_LIMIT_REACHED"),
            ({"company_count": 1}, "COMPANY_LIMIT_REACHED"),
            ({"barrier": "CAPTCHA"}, "USER_INTERVENTION_REQUIRED"),
            ({"state": "PREPARING"}, "INVALID_SUBMISSION_STATE"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                decision = evaluate_submission(self.external_context(**changes), enabled)
                self.assertFalse(decision.allowed)
                self.assertIn(reason, decision.reasons)

        disabled = evaluate_submission(self.external_context(), Policy())
        self.assertFalse(disabled.allowed)
        self.assertIn("EXTERNAL_SUBMISSION_DISABLED", disabled.reasons)

    def test_intent_is_strict_boolean_not_environment_or_target_inference(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "actual_user_intended_job must be a boolean"):
                self.external_context(actual_user_intended_job=value)

        with patch.dict(os.environ, {"MJOBS_EXTERNAL_SUBMISSIONS_ENABLED": "1",
                                     "MJOBS_ACTUAL_USER_INTENT": "1"}, clear=True):
            decision = evaluate_submission(self.external_context(), Policy())
        self.assertFalse(decision.allowed)
        self.assertIn("EXTERNAL_SUBMISSION_DISABLED", decision.reasons)

        for arbitrary_target in ("https://employer.example/apply", "EMPLOYER", "REAL"):
            with self.subTest(target=arbitrary_target), self.assertRaisesRegex(
                    ValueError, "Unknown submission target"):
                self.external_context(target=arbitrary_target)

    def test_malformed_intent_binding_is_rejected_before_evaluation(self):
        cases = (
            ({"job_id": None}, "job_id must be a string"),
            ({"intent_job_id": 1}, "intent_job_id must be a string"),
            ({"intent_receipt_id": False}, "intent_receipt_id must be a string"),
            ({"intent_consumed": "false"}, "intent_consumed must be a boolean"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.external_context(**changes)

    def test_mock_policy_does_not_require_or_infer_live_submission_consent(self):
        mock = replace(
            self.external_context(),
            target="MOCK",
            user_enabled=False,
            actual_user_intended_job=False,
        )
        decision = evaluate_submission(mock, Policy())

        self.assertTrue(decision.allowed)
        self.assertNotIn("USER_ENABLEMENT_REQUIRED", decision.reasons)
        self.assertNotIn("ACTUAL_USER_INTENT_REQUIRED", decision.reasons)
        self.assertNotIn("EXTERNAL_SUBMISSION_DISABLED", decision.reasons)


if __name__ == "__main__":
    unittest.main()
