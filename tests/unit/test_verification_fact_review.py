"""Contract tests for the isolated fact-review acceptance wrapper."""
import unittest
from unittest.mock import Mock

from tools.verification.fact_review import EXPECTED_RED_TESTS, EXPECTED_TESTS, acceptable_result


class FactReviewVerificationTests(unittest.TestCase):
    def test_four_successful_browser_cases_pass(self):
        result = Mock(testsRun=EXPECTED_TESTS, failures=[], errors=[], skipped=[])
        result.wasSuccessful.return_value = True

        self.assertTrue(acceptable_result(result))

    def test_expected_red_requires_three_review_reset_failures_and_demo_case(self):
        result = Mock(testsRun=EXPECTED_TESTS, errors=[], skipped=[])
        result.failures = [(Mock(_testMethodName=name), 'REVIEW_RESET_REQUIRED') for name in EXPECTED_RED_TESTS]

        self.assertTrue(acceptable_result(result, expect_red=True))

    def test_expected_red_rejects_harness_errors(self):
        result = Mock(testsRun=EXPECTED_TESTS, errors=[(None, 'HarnessError')], skipped=[])
        result.failures = [(Mock(_testMethodName=name), 'REVIEW_RESET_REQUIRED') for name in EXPECTED_RED_TESTS]

        self.assertFalse(acceptable_result(result, expect_red=True))

    def test_expected_red_rejects_a_demo_failure_instead_of_a_fact_edit_failure(self):
        failing_tests = [
            Mock(_testMethodName='test_text_edit_requires_fresh_review'),
            Mock(_testMethodName='test_source_edit_requires_fresh_review'),
            Mock(_testMethodName='test_demo_profile_stays_read_only_until_saved_version_loads'),
        ]
        result = Mock(testsRun=EXPECTED_TESTS, errors=[], skipped=[])
        result.failures = [(test, 'REVIEW_RESET_REQUIRED') for test in failing_tests]

        self.assertFalse(acceptable_result(result, expect_red=True))


if __name__ == '__main__':
    unittest.main()
