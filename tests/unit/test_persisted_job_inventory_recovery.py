"""Untrusted/partial page transcripts never establish complete persistence."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cli import compose_recovery as recovery


def transcript():
    from packages.persistence.job_inventory import digest, snapshot_digest
    rows = [dict(job_id=f'job-{index:03}', canonical_sha256='a' * 64, route_count=0,
        source_copy_count=0, source_copies=[], source_copies_sha256=digest([])) for index in range(401)]
    common = dict(schema_version=4, scope='ALL_PERSISTED_JOBS', profile_id='fixture',
        profile_version='b' * 64, total=401, source_copy_count=0,
        snapshot_sha256=snapshot_digest('fixture', 'b' * 64, rows))
    pages = [dict(**common, jobs=rows[:200], after=None, complete=False, next_cursor='page2'),
             dict(**common, jobs=rows[200:400], after=rows[199]['job_id'], complete=False, next_cursor='page3'),
             dict(**common, jobs=rows[400:], after=rows[399]['job_id'], complete=True, next_cursor=None),
             dict(**common, jobs=rows[:1], after=None, complete=False, next_cursor='confirmation')]
    return pages


class PersistedInventoryRecoveryTests(unittest.TestCase):
    def collect(self, pages):
        calls = []
        def get(path):
            calls.append(path)
            return deepcopy(pages[len(calls) - 1])
        return recovery.collect_persisted_job_inventory(get, 'fixture'), calls

    def test_complete_manifest_and_confirmation(self):
        result, calls = self.collect(transcript())
        self.assertEqual(result['job_count'], 401)
        self.assertEqual(result['schema_version'], 4)
        self.assertEqual(parse_qs(urlsplit(calls[-1]).query)['snapshot_sha256'], [result['snapshot_sha256']])
        self.assertIs(recovery.validate_public_job_inventory(result), result)

    def test_missing_duplicate_changed_or_falsely_complete_pages_refused(self):
        for change in ('duplicate', 'missing', 'snapshot', 'profile', 'total', 'cursor', 'empty', 'complete', 'confirm', 'hash'):
            with self.subTest(change=change):
                pages = transcript()
                if change == 'duplicate':
                    pages[1]['jobs'][0] = deepcopy(pages[0]['jobs'][-1])
                elif change == 'missing':
                    pages[1]['jobs'].pop()
                elif change == 'snapshot':
                    pages[1]['snapshot_sha256'] = 'f' * 64
                elif change == 'profile':
                    pages[1]['profile_version'] = 'c' * 64
                elif change == 'total':
                    pages[1]['total'] += 1
                elif change == 'cursor':
                    pages[1]['next_cursor'] = pages[0]['next_cursor']
                elif change == 'empty':
                    pages[1]['jobs'] = []
                elif change == 'complete':
                    pages[0].update(complete=True, next_cursor=None)
                elif change == 'confirm':
                    pages[-1]['snapshot_sha256'] = 'f' * 64
                else:
                    pages[1]['jobs'][0]['canonical_sha256'] = 'f' * 64
                with self.assertRaises(ValueError):
                    self.collect(pages)

    def test_v4_cannot_masquerade_as_v3_or_change_scope(self):
        result, _ = self.collect(transcript())
        for key, value in (('schema_version', 3), ('scope', 'VISIBLE_JOBS'), ('profile_version', 'invalid')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                recovery.validate_public_job_inventory({**result, key: value})

    def test_final_confirmation_cannot_claim_a_partial_page_is_complete(self):
        pages = transcript()
        pages[-1].update(complete=True, next_cursor=None)
        with self.assertRaises(ValueError):
            self.collect(pages)

    def test_baseline_dispatch_uses_v4_without_legacy_fallback(self):
        inventory, _ = self.collect(transcript())
        baseline = dict(profile_id='fixture', job_inventory=inventory)
        with patch.object(recovery, 'collect_persisted_job_inventory', return_value=inventory) as current, \
                patch.object(recovery, 'collect_public_job_inventory', side_effect=AssertionError('No fallback')):
            self.assertEqual(recovery.collect_baseline_job_inventory(None, baseline), inventory)
            current.assert_called_once_with(None, 'fixture')

    def test_recovery_summary_preserves_v4_snapshot_identity(self):
        inventory, _ = self.collect(transcript())
        result = recovery.compare_public_job_inventory({'job_inventory': inventory}, inventory, inventory)
        for key in ('schema_version', 'profile_version', 'snapshot_sha256'):
            self.assertEqual(result[key], inventory[key])
