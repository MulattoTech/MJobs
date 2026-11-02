"""Execute only owned synthetic children; no Docker, network or user data."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from unittest.mock import patch

from infra.deploy.windows import update_mjobs_micro as cd


PRIVATE = 'SYNTHETIC_PRIVATE_COMMAND_VALUE'


class CommandBoundsTests(unittest.TestCase):
    def run_child(self, source, **kwargs):
        return cd.command([sys.executable, '-c', source], **kwargs)

    def assert_safe_failure(self, source, code, **kwargs):
        children = []
        create = subprocess.Popen

        def owned(*args, **options):
            child = create(*args, **options)
            children.append(child)
            return child

        started = time.monotonic()
        with patch.object(cd.subprocess, 'Popen', side_effect=owned):
            with self.assertRaisesRegex(RuntimeError, code) as caught:
                self.run_child(source, **kwargs)
        self.assertLess(time.monotonic() - started, 5)
        self.assertNotIn(PRIVATE, ''.join(traceback.format_exception(caught.exception)))
        for child in children:
            self.assertIsNotNone(child.poll(), 'Owned direct child must be reaped')
            self.assertTrue(child.stdout.closed)
            self.assertTrue(child.stderr.closed)
        return children

    def test_complete_stdout_and_stderr_do_not_deadlock(self):
        source = "import os\nfor _ in range(80):\n os.write(1,b'x'*4096)\n os.write(2,b'y'*4096)"
        self.assertEqual(self.run_child(source, timeout=10), 'x' * (80 * 4096))

    def test_exact_combined_limit_preserves_complete_success(self):
        with patch.object(cd, 'MAX_COMMAND_OUTPUT_BYTES', 4096):
            result = self.run_child("import os;os.write(1,b'x'*2048);os.write(2,b'y'*2048)")
        self.assertEqual(result, 'x' * 2048)

    def test_stdout_and_stderr_overflow_stop_owned_child_without_later_work(self):
        for fd in (1, 2):
            with self.subTest(stream=fd), tempfile.TemporaryDirectory() as temporary:
                marker = Path(temporary) / 'must-not-exist'
                source = (f"import os,time,pathlib;os.write({fd},{PRIVATE.encode()!r}*1000);"
                          f"time.sleep(10);pathlib.Path({str(marker)!r}).write_text('late')")
                with patch.object(cd, 'MAX_COMMAND_OUTPUT_BYTES', 4096):
                    self.assert_safe_failure(source, 'COMMAND_OUTPUT_LIMIT')
                self.assertFalse(marker.exists())

    def test_fast_exit_cannot_hide_oversized_output(self):
        with patch.object(cd, 'MAX_COMMAND_OUTPUT_BYTES', 4096):
            self.assert_safe_failure("import os;os.write(1,b'x'*8192)", 'COMMAND_OUTPUT_LIMIT')

    def test_stalled_child_times_out_and_is_reaped(self):
        self.assert_safe_failure(f"import time;secret={PRIVATE!r};time.sleep(10)",
                                 'COMMAND_TIMEOUT', timeout=.2)

    def test_cancellation_stops_owned_child(self):
        started = time.monotonic()
        self.assert_safe_failure("import time;time.sleep(10)", 'COMMAND_CANCELLED',
                                 timeout=10, cancelled=lambda: time.monotonic() - started > .2)

    def test_pre_cancelled_command_does_not_start(self):
        with patch.object(cd.subprocess, 'Popen') as create:
            with self.assertRaisesRegex(RuntimeError, 'COMMAND_CANCELLED'):
                self.run_child('pass', cancelled=lambda: True)
        create.assert_not_called()

    def test_interruption_cleans_up_before_propagation(self):
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt()
            return False

        children = []
        create = subprocess.Popen

        def owned(*args, **kwargs):
            child = create(*args, **kwargs)
            children.append(child)
            return child

        with patch.object(cd.subprocess, 'Popen', side_effect=owned):
            with self.assertRaises(KeyboardInterrupt):
                self.run_child('import time;time.sleep(10)', cancelled=cancel)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stderr.closed)

    def test_inherited_open_pipes_cannot_extend_deadline(self):
        # The second child is explicitly owned by this test and cleaned up below.
        # Its inherited handles outlive the command parent, reproducing a classic
        # communicate()/reader-thread timeout hang without selecting any user PID.
        with tempfile.TemporaryDirectory() as temporary:
            child_file = Path(temporary) / 'child.json'
            child_source = 'import time;time.sleep(2)'
            source = ("import subprocess,sys,json,pathlib;"
                      f"p=subprocess.Popen([sys.executable,'-c',{child_source!r}]);"
                      f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid))")
            started = time.monotonic()
            self.assert_safe_failure(source, 'COMMAND_TIMEOUT', timeout=.5)
            self.assertLess(time.monotonic() - started, 2)
            # The finite synthetic descendant exits on its own; no arbitrary PID
            # termination is used. All helper-owned pipe handles are already closed.

    def test_known_backup_refusal_and_private_stderr_are_sanitized(self):
        source = ("import sys;sys.stdout.write(" + repr(PRIVATE) + ");"
                  "sys.stderr.write('Backup refused: PostgreSQL must be running for a logical backup\\n');"
                  "sys.exit(2)")
        self.assert_safe_failure(source, 'BACKUP_POSTGRES_NOT_RUNNING', backup_diagnostics=True)

    def test_invalid_deadline_and_start_failure_do_not_expose_paths(self):
        for timeout in (0, -1, True, float('nan'), float('inf'), 7201, 10**1000):
            with self.subTest(timeout=timeout), patch.object(cd.subprocess, 'Popen') as create:
                with self.assertRaisesRegex(RuntimeError, 'COMMAND_TIMEOUT_INVALID'):
                    self.run_child('pass', timeout=timeout)
                create.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'COMMAND_START_FAILED') as caught:
            cd.command([str(Path(tempfile.gettempdir()) / PRIVATE / 'missing-tool')])
        self.assertNotIn(PRIVATE, ''.join(traceback.format_exception(caught.exception)))


if __name__ == '__main__':
    unittest.main()
