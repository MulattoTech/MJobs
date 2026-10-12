"""Read-only OS identity checks must distinguish exit from uncertain ownership."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from services.applications.browser_process import capture_browser_identity, process_status, _read_process


class BrowserProcessIdentityTests(unittest.TestCase):
    def test_linux_process_start_field_handles_parentheses_and_spaces_in_name(self):
        stat = "123 (worker ) with spaces) S " + " ".join(str(field) for field in range(4, 53))
        with patch("services.applications.browser_process.sys.platform", "linux"), \
                patch("pathlib.Path.read_text", return_value=stat):
            self.assertEqual(_read_process(123), ("22", "LIVE"))
        with patch("services.applications.browser_process.sys.platform", "linux"), \
                patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
            self.assertIsNone(_read_process(123))

    def test_real_owned_child_live_then_exit_without_terminating_any_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE)
        try:
            with tempfile.TemporaryDirectory() as directory:
                context = Mock()
                session = context.browser.new_browser_cdp_session.return_value
                session.send.side_effect = [
                    {"arguments": ["chromium", "--user-data-dir=" + directory]},
                    {"processInfo": [{"type": "browser", "id": child.pid}]},
                ]
                identity = capture_browser_identity(context, Path(directory))
            self.assertEqual(process_status(identity), "LIVE")
            child.stdin.close()
            child.wait(timeout=5)
            self.assertEqual(process_status(identity), "EXITED")
            session.detach.assert_called_once()
        finally:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            child.wait(timeout=5)

    def test_pid_reuse_foreign_host_and_unreadable_identity_are_unknown(self):
        identity = {"pid": 123, "created": "111", "host": "a" * 64, "profile_sha256": "c" * 64}
        with patch("services.applications.browser_process._host_identity", return_value="a" * 64), \
                patch("services.applications.browser_process._read_process", return_value=("222", "LIVE")):
            self.assertEqual(process_status(identity), "UNKNOWN")
        with patch("services.applications.browser_process._host_identity", return_value="b" * 64):
            self.assertEqual(process_status(identity), "UNKNOWN")
        with patch("services.applications.browser_process._host_identity", return_value="a" * 64), \
                patch("services.applications.browser_process._read_process", side_effect=PermissionError):
            self.assertEqual(process_status(identity), "UNKNOWN")

    def test_capture_rejects_wrong_profile_and_ambiguous_root_without_exposing_arguments(self):
        for arguments, processes in [
            (["chromium", "--user-data-dir=/wrong/profile"], [{"type": "browser", "id": os.getpid()}]),
            (["chromium", "--user-data-dir=/expected"], [{"type": "browser", "id": 1}, {"type": "browser", "id": 2}]),
        ]:
            with self.subTest(arguments=arguments):
                context = Mock()
                context.browser.new_browser_cdp_session.return_value.send.side_effect = [
                    {"arguments": arguments}, {"processInfo": processes}]
                with self.assertRaisesRegex(ValueError, "BROWSER_IDENTITY_UNAVAILABLE"):
                    capture_browser_identity(context, Path("/expected"))

    def test_malformed_identity_never_proves_exit(self):
        for identity in (None, {}, {"pid": True}, {"pid": -1},
                         {"pid": 1, "created": "x", "host": "a" * 64}):
            with self.subTest(identity=identity):
                self.assertEqual(process_status(identity), "UNKNOWN")
