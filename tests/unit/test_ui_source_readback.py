"""The live-source UI oracle accepts only safe preparation or explicit missing evidence."""
import unittest

from cli.ui_source_readback import classify_public_preparation


class PublicSourceReadbackTests(unittest.TestCase):
    def test_prepared_public_job_must_remain_manual_only(self):
        application = {"id": "synthetic-app", "target": "REAL", "state": "PREPARED"}
        result = classify_public_preparation([application], "")
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(result["application"], application)

    def test_missing_responsibility_evidence_must_block_without_application(self):
        for state in ('FAILED', 'DEAD_LETTERED'):
            with self.subTest(state=state):
                result = classify_public_preparation([], state + ": COVER_LETTER_JOB_CONTEXT_MISSING")
                self.assertEqual(result["status"], "BLOCKED_MISSING_JOB_CONTEXT")
                self.assertIsNone(result["application"])
                self.assertIn("no document or application was created", result["reason"])

    def test_known_browser_alert_wrapper_preserves_exact_missing_context_block(self):
        result = classify_public_preparation(
            [], "Action needs attention\nFAILED: COVER_LETTER_JOB_CONTEXT_MISSING"
        )
        self.assertEqual(result["status"], "BLOCKED_MISSING_JOB_CONTEXT")

    def test_unexpected_failure_or_application_state_does_not_pass_the_oracle(self):
        with self.assertRaises(AssertionError):
            classify_public_preparation([], "FAILED: DOCUMENT_RENDER_ERROR")
        with self.assertRaises(AssertionError):
            classify_public_preparation(
                [],
                "FAILED: COVER_LETTER_JOB_CONTEXT_MISSING; FAILED: DOCUMENT_RENDER_ERROR",
            )
        with self.assertRaises(AssertionError):
            classify_public_preparation(
                [],
                "Action needs attention\nFAILED: COVER_LETTER_JOB_CONTEXT_MISSING\nFAILED: DOCUMENT_RENDER_ERROR",
            )
        with self.assertRaises(AssertionError):
            classify_public_preparation([{"id": "x", "target": "REAL", "state": "REVIEW_REQUIRED"}], "")
        with self.assertRaises(AssertionError):
            classify_public_preparation([{"id": "x", "target": "MOCK", "state": "PREPARED"}], "")


if __name__ == "__main__":
    unittest.main()
