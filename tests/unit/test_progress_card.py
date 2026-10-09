"""Sanity, safety, and idempotency checks for the MJobs status card."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.verification.progress_card import card_current, render_json, render_svg, write_card


class ProgressCardTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            'original': {'PASS': 1, 'PARTIAL': 1, 'NOT_RUN': 1}, 'original_total': 3,
            'reference': {'PASS': 1, 'PARTIAL': 0, 'NOT_RUN': 1}, 'reference_total': 2,
            'original_as_of': '2026-10-09T20:30:32Z',
            'reference_as_of': '2026-10-08T20:30:32Z',
            'milestone': 'Feature A', 'release': 'UNKNOWN',
        }

    def test_json_counts_and_percent(self):
        result = json.loads(render_json(self.state))
        self.assertEqual(result['original']['pass'], 1)
        self.assertEqual(result['original']['partial'], 1)
        self.assertEqual(result['original']['not_run'], 1)
        self.assertEqual(result['original']['accepted_percent'], 33.3)
        self.assertEqual(result['reference']['total'], 2)
        self.assertEqual(result['schema_version'], 1)

    def test_svg_is_readable_and_escapes_untrusted_text(self):
        self.state['original_as_of'] = '<fake&>'
        svg = render_svg(self.state)
        self.assertIn('1 / 3', svg)
        self.assertIn('33.3%', svg)
        self.assertNotIn('<fake&>', svg)
        self.assertIn('&lt;fake&amp;&gt;', svg)

    def test_checks_are_read_only_and_writes_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(card_current(root, self.state))
            self.assertFalse((root / 'docs').exists())
            self.assertTrue(write_card(root, self.state))
            target = root / 'docs/PROGRESS_CARD.svg'
            mtime = target.stat().st_mtime_ns
            self.assertTrue(card_current(root, self.state))
            self.assertFalse(write_card(root, self.state))
            self.assertEqual(target.stat().st_mtime_ns, mtime)
            self.state['original']['PASS'] = 2
            self.state['original']['PARTIAL'] = 0
            self.assertFalse(card_current(root, self.state))
            self.assertTrue(write_card(root, self.state))
            self.assertTrue(card_current(root, self.state))

    def test_reject_inconsistent_totals(self):
        self.state['original_total'] = 4
        with self.assertRaisesRegex(ValueError, 'Inconsistent'):
            render_json(self.state)


if __name__ == '__main__':
    unittest.main()
