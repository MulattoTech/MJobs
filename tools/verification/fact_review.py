"""Run the isolated fact-review browser oracle and emit inspectable CI evidence."""
import argparse
from datetime import datetime, timezone
import io
import json
import platform
from pathlib import Path
import re
import subprocess
import unittest


EXPECTED_TESTS = 4
EXPECTED_RED_TESTS = frozenset({
    'test_text_edit_requires_fresh_review',
    'test_source_edit_requires_fresh_review',
    'test_kind_edit_requires_fresh_review',
})
GIT_REVISION_TIMEOUT = 5
_GIT_REVISION = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def acceptable_result(result, expect_red=False):
    failed_names = {getattr(test, '_testMethodName', '') for test, _ in result.failures}
    red_confirmed = (result.testsRun == EXPECTED_TESTS and len(result.failures) == len(EXPECTED_RED_TESTS)
                     and failed_names == EXPECTED_RED_TESTS and not result.errors
                     and not result.skipped and all('REVIEW_RESET_REQUIRED' in text for _, text in result.failures))
    return red_confirmed if expect_red else result.wasSuccessful() and result.testsRun == EXPECTED_TESTS and not result.skipped


def _git_output(arguments, timeout_category, failure_category):
    try:
        result = subprocess.run(
            ['git', *arguments], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            timeout=GIT_REVISION_TIMEOUT, shell=False, check=False)
    except subprocess.TimeoutExpired:
        return None, timeout_category
    except OSError:
        return None, 'GIT_UNAVAILABLE'
    if result.returncode != 0:
        return None, failure_category
    return result.stdout or '', None


def resolve_source_commit():
    """Return a validated clean HEAD SHA or a stable, safe failure category."""
    revision, error = _git_output(['rev-parse', 'HEAD'], 'GIT_TIMEOUT', 'GIT_REVISION_FAILED')
    if error:
        return None, error
    commit = revision.strip()
    if not _GIT_REVISION.fullmatch(commit):
        return None, 'GIT_REVISION_INVALID'
    status, error = _git_output(
        ['status', '--porcelain=v1', '--untracked-files=all'],
        'GIT_STATUS_TIMEOUT', 'GIT_STATUS_FAILED')
    if error:
        return None, error
    if status.strip():
        return None, 'GIT_WORKTREE_DIRTY'
    return commit, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expect-red', action='store_true', help='Require the baseline UI review-boundary failures, not harness errors')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromName('tests.browser.test_fact_review')
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print(stream.getvalue(), end='')
    source_commit, source_commit_error = resolve_source_commit()
    passed = acceptable_result(result, expect_red=args.expect_red) and source_commit_error is None
    evidence = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'result': 'PASS' if passed else 'FAIL',
        'mode': 'BASELINE_EXPECTED_FAILURE' if args.expect_red else 'BROWSER_ACCEPTANCE',
        'command': 'python -m tools.verification.fact_review'+(' --expect-red' if args.expect_red else ''),
        'target': 'Isolated synthetic Chromium, local API, temporary migrated SQLite, existing operation worker and actual document downloads',
        'source_commit': source_commit,
        'source_commit_error_category': source_commit_error,
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'fields': ['text', 'source', 'kind', 'fixture_demo_load'],
        'execution_platform': platform.system(),
        'data_scope': 'Synthetic fixtures and temporary database/documents only',
        'external_application_submission_performed': False,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
