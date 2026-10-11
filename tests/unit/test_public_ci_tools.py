"""Offline regression tests for public-repository verification helpers."""
from pathlib import Path
import tempfile
import unittest
from tools.ci.check_repository import check_markdown, check_workflows
from tools.ci.run_shard import select_files
from tools.ci.test_summary import summarize
from tools.ci.release_gate import unmet


class PublicCITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_links_check_real_targets_without_network(self):
        (self.root / 'README.md').write_text('[good](there.md) [web](https://example.com) [bad](absent.md)', encoding='utf-8')
        (self.root / 'there.md').write_text('# here', encoding='utf-8')
        result = check_markdown(self.root, ['README.md'])
        self.assertEqual(len(result), 1)
        self.assertIn('absent.md', result[0])

    def test_links_cannot_escape_checkout(self):
        (self.root / 'README.md').write_text('[bad](../outside.md)', encoding='utf-8')
        self.assertTrue(check_markdown(self.root, ['README.md']))

    def test_shards_cover_every_test_once(self):
        folder = self.root / 'tests/browser'
        folder.mkdir(parents=True)
        for number in range(11):
            (folder / f'test_{number:02}.py').write_text('')
        groups = [select_files(self.root, 'browser', index, 4) for index in range(4)]
        flat = [item for group in groups for item in group]
        self.assertEqual(len(flat), 11)
        self.assertEqual(len(set(flat)), 11)
        self.assertEqual(groups[0], select_files(self.root, 'browser', 0, 4))

    def test_invalid_shards_rejected(self):
        for args in [('browser', 4, 4), ('unknown', 0, 1), ('browser', -1, 4)]:
            with self.assertRaises(ValueError):
                select_files(self.root, *args)

    def test_junit_counts_keep_failures_and_skips(self):
        path = self.root / 'x.xml'
        path.write_text('<testsuites><testsuite tests="10" failures="2" errors="1" skipped="3"/></testsuites>')
        self.assertEqual(summarize(path), {'tests': 10, 'failures': 2, 'errors': 1, 'skipped': 3})

    def test_privileged_or_unpinned_workflow_is_rejected(self):
        folder = self.root / '.github/workflows'
        folder.mkdir(parents=True)
        (folder / 'bad.yml').write_text('on: pull_request_target\npermissions: {contents: write}\njobs:\n  bad:\n    runs-on: self-hosted\n    steps:\n      - uses: actions/checkout@main\n')
        errors = check_workflows(self.root)
        self.assertTrue(any('privileged' in error for error in errors))
        self.assertTrue(any('SHA-pinned' in error for error in errors))
        self.assertTrue(any('personal runner' in error for error in errors))

    def test_release_gate_rejects_partial_and_empty_acceptance(self):
        rows = [{'id': 'A', 'priority': 'P0', 'status': 'PASS'},
                {'id': 'B', 'priority': 'P1', 'status': 'PARTIAL'},
                {'id': 'C', 'priority': 'P4', 'status': 'NOT_RUN'}]
        self.assertEqual(unmet(rows, 'p0p1'), ['B'])
        self.assertEqual(unmet(rows, 'all'), ['B', 'C'])
        self.assertTrue(unmet([], 'all'))

    def test_safe_readonly_workflow_is_accepted(self):
        folder = self.root / '.github/workflows'
        folder.mkdir(parents=True)
        (folder / 'good.yml').write_text('on: pull_request\npermissions: {contents: read}\njobs:\n  good:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n    steps:\n      - run: echo pass\n')
        self.assertEqual(check_workflows(self.root), [])


if __name__ == '__main__':
    unittest.main()
