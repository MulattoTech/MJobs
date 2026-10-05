"""Run the isolated fact-review browser oracle and emit inspectable CI evidence."""
import argparse
from datetime import datetime, timezone
import io
import json
import platform
from pathlib import Path
import subprocess
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expect-red', action='store_true', help='Require the baseline UI review-boundary failures, not harness errors')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromName('tests.browser.test_fact_review')
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print(stream.getvalue(), end='')
    red_confirmed = (result.testsRun == 3 and len(result.failures) == 3 and not result.errors
                     and not result.skipped and all('REVIEW_RESET_REQUIRED' in text for _, text in result.failures))
    passed = red_confirmed if args.expect_red else result.wasSuccessful() and result.testsRun == 3 and not result.skipped
    evidence = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'result': 'PASS' if passed else 'FAIL',
        'mode': 'BASELINE_EXPECTED_FAILURE' if args.expect_red else 'BROWSER_ACCEPTANCE',
        'command': 'python -m tools.verification.fact_review'+(' --expect-red' if args.expect_red else ''),
        'target': 'Isolated synthetic Chromium, local API, temporary migrated SQLite, existing operation worker and actual document downloads',
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'fields': ['text', 'source', 'kind'],
        'execution_platform': platform.system(),
        'data_scope': 'Synthetic fixtures and temporary database/documents only',
        'external_application_submission_performed': False,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2)+'\n', encoding='utf-8')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
