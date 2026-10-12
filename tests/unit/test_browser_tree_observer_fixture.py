"""A fixture must publish the same native snapshot that decides its exit."""
import io
import unittest
from unittest.mock import Mock, patch

from tests.fixtures import browser_tree_observer as fixture


class ObserverFixtureTests(unittest.TestCase):
    def test_snapshot_replace_persistent_lock_raises_at_deadline(self):
        with patch.object(fixture.Path, 'write_text'), \
                patch.object(fixture.Path, 'replace', side_effect=PermissionError('synthetic reader lock')), \
                patch.object(fixture.time, 'monotonic', side_effect=[0, 1]):
            with self.assertRaises(PermissionError):
                fixture.write(fixture.Path('synthetic/observation.json'), {'status': 'LIVE'})

    def test_snapshot_replace_retries_temporary_windows_reader_lock(self):
        with patch.object(fixture.Path, 'write_text'), \
                patch.object(fixture.Path, 'replace', side_effect=[PermissionError('synthetic reader lock'), None]) as replace, \
                patch.object(fixture.time, 'sleep'):
            fixture.write(fixture.Path('synthetic/observation.json'), {'status': 'LIVE'})
        self.assertEqual(replace.call_count, 2)

    def test_live_to_exited_transition_publishes_final_zero_active_snapshot(self):
        query = Mock()
        query.status.side_effect = ['LIVE', 'EXITED', 'EXITED']
        query.accounting.side_effect = [
            {'total': 1, 'active': 1, 'terminated': 0},
            {'total': 1, 'active': 0, 'terminated': 0}]
        snapshots = []
        def write(path, value):
            if path.name == 'observation.json':
                snapshots.append(value)
        with patch.object(fixture.sys, 'argv', ['fixture', 'observer', 'synthetic', 'browser']), \
                patch.object(fixture.sys, 'stdin', io.StringIO('1\n')), \
                patch.object(fixture, '_kernel'), \
                patch.object(fixture, 'BrowserTreeQuery', return_value=query), \
                patch.object(fixture, 'write', side_effect=write), \
                patch.object(fixture.time, 'sleep'), \
                patch.object(fixture.Path, 'exists', return_value=False):
            fixture.main()
        self.assertEqual(snapshots[-1], {'status': 'EXITED',
            'accounting': {'total': 1, 'active': 0, 'terminated': 0}})
        query.close.assert_called_once()
