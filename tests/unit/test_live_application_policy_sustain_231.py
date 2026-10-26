"""MJ-REQ-230: real submission needs distinct, explicit user intent."""

import unittest

from packages.domain.safety import Policy, SubmissionContext, evaluate_submission


class LiveApplicationPolicyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
