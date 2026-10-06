"""Contract tests for the isolated fact-review acceptance wrapper."""
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

from tools.verification.fact_review import (
    EXPECTED_RED_TESTS, EXPECTED_TESTS, acceptable_result, main, resolve_source_commit,
)


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

    def test_source_commit_command_has_a_deadline_and_suppresses_diagnostics(self):
        commit = 'a' * 40
        revision = subprocess.CompletedProcess([], 0, stdout=commit + '\n')
        clean = subprocess.CompletedProcess([], 0, stdout='')
        with patch('tools.verification.fact_review.subprocess.run', side_effect=[revision, clean]) as run:
            self.assertEqual(resolve_source_commit(), (commit, None))
        self.assertEqual(run.call_count, 2)
        for call, command in zip(run.call_args_list,
                (['git', 'rev-parse', 'HEAD'], ['git', 'status', '--porcelain=v1', '--untracked-files=all'])):
            self.assertEqual(call.args[0], command)
            self.assertEqual(call.kwargs['timeout'], 5)
            self.assertFalse(call.kwargs['shell'])
            self.assertIs(call.kwargs['stdout'], subprocess.PIPE)
            self.assertIs(call.kwargs['stderr'], subprocess.DEVNULL)

    def test_source_commit_launch_exit_and_invalid_sha_errors_are_classified(self):
        cases = [
            (OSError('private executable path'), 'GIT_UNAVAILABLE'),
            (subprocess.CompletedProcess([], 2, stdout='private stderr-like detail'),
             'GIT_REVISION_FAILED'),
            (subprocess.CompletedProcess([], 0, stdout='private repository path'),
             'GIT_REVISION_INVALID'),
        ]
        for outcome, category in cases:
            options = ({'side_effect': outcome} if isinstance(outcome, BaseException)
                       else {'return_value': outcome})
            with self.subTest(category=category), \
                 patch('tools.verification.fact_review.subprocess.run', **options):
                self.assertEqual(resolve_source_commit(), (None, category))

    def test_dirty_worktree_cannot_be_certified_with_only_the_prior_head_sha(self):
        head = subprocess.CompletedProcess([], 0, stdout='a' * 40 + '\n')
        dirty = subprocess.CompletedProcess([], 0, stdout=' M tools/verification/fact_review.py\n?? local-output.json\n')
        with patch('tools.verification.fact_review.subprocess.run', side_effect=[head, dirty]) as run:
            self.assertEqual(resolve_source_commit(), (None, 'GIT_WORKTREE_DIRTY'))
        self.assertEqual(run.call_count, 2)

    def test_worktree_status_timeout_launch_and_exit_errors_fail_closed(self):
        revision = subprocess.CompletedProcess([], 0, stdout='b' * 40 + '\n')
        cases = [
            (subprocess.TimeoutExpired('git status', 5), 'GIT_STATUS_TIMEOUT'),
            (OSError('private git executable path'), 'GIT_UNAVAILABLE'),
            (subprocess.CompletedProcess([], 1, stdout='private path'), 'GIT_STATUS_FAILED'),
        ]
        for status, category in cases:
            with self.subTest(category=category), \
                 patch('tools.verification.fact_review.subprocess.run',
                       side_effect=[revision, status]):
                self.assertEqual(resolve_source_commit(), (None, category))

    def test_source_commit_timeout_is_safe_and_evidence_is_still_written(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'fact-review.json'
            runner = Mock()
            successful = Mock(testsRun=EXPECTED_TESTS, failures=[], errors=[], skipped=[])
            successful.wasSuccessful.return_value = True
            runner.run.return_value = successful
            timeout = subprocess.TimeoutExpired('git', 5, stderr=b'private repository path')
            with patch.object(sys, 'argv', ['fact-review', '--evidence', str(evidence)]), \
                 patch('tools.verification.fact_review.unittest.defaultTestLoader.loadTestsFromName', return_value=object()), \
                 patch('tools.verification.fact_review.unittest.TextTestRunner', return_value=runner), \
                 patch('tools.verification.fact_review.subprocess.run', side_effect=timeout):
                self.assertEqual(main(), 1)

            saved = json.loads(evidence.read_text(encoding='utf-8'))
            self.assertEqual(saved['result'], 'FAIL')
            self.assertIsNone(saved['source_commit'])
            self.assertEqual(saved['source_commit_error_category'], 'GIT_TIMEOUT')
            self.assertNotIn('private repository path', str(saved))


if __name__ == '__main__':
    unittest.main()
