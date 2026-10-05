"""Contract tests for the isolated fact-review acceptance wrapper."""
import unittest
from unittest.mock import Mock

from tools.verification.fact_review import EXPECTED_TESTS, acceptable_result


class FactReviewVerificationTests(unittest.TestCase):
    def test_four_successful_browser_cases_pass(self):
        result = Mock(testsRun=EXPECTED_TESTS, failures=[], errors=[], skipped=[])
        result.wasSuccessful.return_value = True

        self.assertTrue(acceptable_result(result))

    def test_expected_red_requires_three_review_reset_failures_and_demo_case(self):
        result = Mock(testsRun=EXPECTED_TESTS, errors=[], skipped=[])
        result.failures = [(None, 'REVIEW_RESET_REQUIRED') for _ in range(3)]

        self.assertTrue(acceptable_result(result, expect_red=True))

    def test_expected_red_rejects_harness_errors(self):
        result = Mock(testsRun=EXPECTED_TESTS, errors=[(None, 'HarnessError')], skipped=[])
        result.failures = [(None, 'REVIEW_RESET_REQUIRED') for _ in range(3)]

        self.assertFalse(acceptable_result(result, expect_red=True))


if __name__ == '__main__':
    unittest.main()
