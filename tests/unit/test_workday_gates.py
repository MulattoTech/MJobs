"""Pure Workday authentication and answer gate classification tests."""
import unittest

from services.applications.adapters.workday_gates import classify_workday_gate


class WorkdayGateTests(unittest.TestCase):
    def test_authentication_required_holds_without_resume(self):
        self.assertEqual(
            classify_workday_gate(authentication_required=True),
            {"status": "AUTHENTICATION_REQUIRED", "reason": "USER_AUTHENTICATION_REQUIRED"},
        )

    def test_user_completed_authentication_resumes_only_after_auth_gate_clears(self):
        resumed = classify_workday_gate(
            authentication_was_required=True,
            user_completed_authentication=True,
            answer_known=True,
        )
        still_required = classify_workday_gate(
            authentication_required=True,
            authentication_was_required=True,
            user_completed_authentication=True,
            answer_known=True,
        )
        unsolicited = classify_workday_gate(
            user_completed_authentication=True,
            answer_known=True,
        )

        self.assertEqual(resumed, {"status": "SESSION_RESUMED", "reason": "USER_AUTHENTICATION_COMPLETED"})
        self.assertEqual(still_required["status"], "AUTHENTICATION_REQUIRED")
        self.assertEqual(unsolicited["status"], "READY_FOR_REVIEW")
        self.assertEqual(
            classify_workday_gate(authentication_was_required=True, answer_known=True),
            {"status": "HUMAN_REVIEW_REQUIRED", "reason": "USER_RESUME_UNCONFIRMED"},
        )

    def test_unknown_or_sensitive_answers_require_human_review(self):
        for args, reason in (
            ({"answer_known": False}, "UNKNOWN_ANSWER"),
            ({"answer_known": True, "sensitive": True}, "SENSITIVE_ANSWER"),
            ({"sensitive": True}, "SENSITIVE_ANSWER"),
        ):
            with self.subTest(reason=reason, args=args):
                self.assertEqual(
                    classify_workday_gate(**args),
                    {"status": "HUMAN_REVIEW_REQUIRED", "reason": reason},
                )

    def test_changed_or_unsupported_questions_require_human_review(self):
        for args, reason in (
            ({"question_changed": True}, "QUESTION_CHANGED"),
            ({"question_supported": False}, "QUESTION_UNSUPPORTED"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    classify_workday_gate(**args),
                    {"status": "HUMAN_REVIEW_REQUIRED", "reason": reason},
                )

    def test_changed_question_takes_precedence_over_answer_classification(self):
        result = classify_workday_gate(
            answer_known=True,
            sensitive=True,
            question_changed=True,
        )
        self.assertEqual(result, {"status": "HUMAN_REVIEW_REQUIRED", "reason": "QUESTION_CHANGED"})

    def test_malformed_flags_fail_closed(self):
        result = classify_workday_gate(authentication_required="false")
        self.assertEqual(result, {"status": "HUMAN_REVIEW_REQUIRED", "reason": "INVALID_GATE_STATE"})

    def test_user_resume_does_not_override_unknown_or_sensitive_answers(self):
        result = classify_workday_gate(
            authentication_was_required=True,
            user_completed_authentication=True,
            answer_known=False,
        )
        self.assertEqual(result, {"status": "HUMAN_REVIEW_REQUIRED", "reason": "UNKNOWN_ANSWER"})


if __name__ == "__main__":
    unittest.main()
