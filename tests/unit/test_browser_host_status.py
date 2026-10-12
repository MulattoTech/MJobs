"""Host diagnostics must use retained launch identity, never a reusable PID."""
from threading import RLock
from unittest.mock import Mock, patch
import sys
import unittest

from services.applications import browser_tree
from tests.unit.test_browser_tree_containment import eventually


class BrowserHostStatusTests(unittest.TestCase):
    def test_retained_handle_is_queried_without_reopening_a_pid(self):
        job = object.__new__(browser_tree.BrowserTreeJob)
        job._lock = RLock()
        job._host_handle = 123
        job._kernel = Mock()
        def exit_code(handle, value):
            self.assertEqual(handle, 123)
            value._obj.value = 7
            return True
        job._kernel.GetExitCodeProcess.side_effect = exit_code
        self.assertTrue(callable(getattr(job, 'host_exit_code', None)),
                        'Host status must query the retained launch handle')
        self.assertEqual(job.host_exit_code(), 7)
        job._kernel.OpenProcess.assert_not_called()
        job._kernel.GetExitCodeProcess.return_value = False
        job._kernel.GetExitCodeProcess.side_effect = None
        self.assertIsNone(job.host_exit_code())
        job._host_handle = None
        self.assertIsNone(job.host_exit_code())

    def test_close_releases_retained_handle_once_without_killing_host(self):
        job = object.__new__(browser_tree.BrowserTreeJob)
        job._lock = RLock()
        job._host_handle, job._handle = 123, 456
        job._kernel = Mock()
        job.close()
        job.close()
        self.assertEqual([c.args for c in job._kernel.CloseHandle.call_args_list],
                         [(123,), (456,)])
        job._kernel.TerminateProcess.assert_not_called()
        self.assertIsNone(job.host_exit_code())

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows retained identity')
    def test_fast_exit_keeps_original_status_even_when_pid_reopen_lies(self):
        job = browser_tree.BrowserTreeJob()
        query = job.query_handle()
        try:
            self.assertTrue(callable(getattr(job, 'host_exit_code', None)),
                            'Host status must retain the launch process handle')
            with patch.object(job._kernel, 'DuplicateHandle',
                              wraps=job._kernel.DuplicateHandle) as duplicate:
                job.launch([sys.executable, '-c', 'raise SystemExit(7)'])
                duplicate.assert_called_once()
                self.assertEqual(duplicate.call_args.args[4:], (0x1000, False, 0))
            eventually(lambda: query.status() == 'EXITED')
            # A reopened PID could refer to a foreign successful process.
            # No such reopen is allowed, regardless of what it would return.
            with patch.object(job._kernel, 'OpenProcess', return_value=999) as reopen:
                self.assertEqual(job.host_exit_code(), 7)
                reopen.assert_not_called()
            self.assertEqual(query.accounting()['terminated'], 0)
            job.close()
            self.assertIsNone(job.host_exit_code())
            self.assertEqual(query.status(), 'EXITED')
        finally:
            query.close()
            job.close()

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows retained identity')
    def test_failed_host_handle_retention_is_ambiguous_and_never_replayed(self):
        job = browser_tree.BrowserTreeJob()
        query = job.query_handle()
        try:
            with patch.object(job._kernel, 'DuplicateHandle', return_value=False):
                with self.assertRaisesRegex(ValueError, 'TREE_LAUNCH_UNAVAILABLE'):
                    job.launch([sys.executable, '-c', 'raise SystemExit(7)'])
            eventually(lambda: query.status() == 'EXITED')
            self.assertIsNone(job.host_exit_code())
            self.assertEqual(query.accounting()['terminated'], 0)
            with self.assertRaisesRegex(ValueError, 'TREE_LAUNCH_UNAVAILABLE'):
                job.launch([sys.executable, '-c', 'pass'])
        finally:
            query.close()
            job.close()
