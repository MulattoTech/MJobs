"""Private host input must fail closed before any browser activity."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ManualAuthHostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.profile = self.root / "app1" / "browser-profile"
        self.profile.mkdir(parents=True)
        self.payload = {"schema": 1, "output_dir": str(self.root),
                        "application_id": "app1", "mock_origin": "http://127.0.0.1:8123",
                        "requisition": "job_1", "budget_seconds": 30}

    def run_host(self, data):
        from services.applications import manual_auth_host as host
        with patch.object(host, "run_browser", return_value="MANUAL_AUTH_WINDOW_CLOSED") as browser:
            result = host.run(io.BytesIO(data))
        return result, browser

    def test_valid_private_input_uses_exact_profile_and_generated_login(self):
        result, browser = self.run_host(json.dumps(self.payload).encode())
        self.assertEqual(result, 0)
        browser.assert_called_once()
        args, kwargs = browser.call_args
        self.assertEqual(args[0], self.profile)
        self.assertEqual(args[1], "http://127.0.0.1:8123/ats/login/job_1")
        self.assertTrue(args[2].allowed(args[1]))
        self.assertIn("check", kwargs)

    def test_invalid_input_never_starts_browser(self):
        changes = [{"schema": True}, {"schema": 2}, {"application_id": "../app1"},
                   {"output_dir": "."}, {"application_id": "missing"},
                   {"mock_origin": "https://example.com"},
                   {"mock_origin": "http://127.0.0.1:8123/?private=1"},
                   {"mock_origin": "http://user:secret@127.0.0.1:8123"},
                   {"requisition": "../apply"}, {"budget_seconds": True},
                   {"budget_seconds": 0}, {"budget_seconds": 901},
                   {"secret": "extra-field"}]
        for change in changes:
            with self.subTest(change=change):
                result, browser = self.run_host(json.dumps({**self.payload, **change}).encode())
                self.assertEqual(result, 2)
                browser.assert_not_called()
        for data in (b"", b"[1]", b"null", b"\xff", b"{}\0", b"x" * 2049,
                     json.dumps(self.payload).encode()[:-1] + b',"schema":1}'):
            with self.subTest(data=data[:20]):
                result, browser = self.run_host(data)
                self.assertEqual(result, 2)
                browser.assert_not_called()

    def test_escaped_profile_is_rejected(self):
        from services.applications import manual_auth_host as host
        # Model the resolved symlink/junction destination without requiring
        # Windows symlink privileges on the test workstation.
        real_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == self.profile:
                return self.root.parent / "foreign-profile"
            return real_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            result, browser = self.run_host(json.dumps(self.payload).encode())
        self.assertEqual(result, 2)
        browser.assert_not_called()

    def test_exit_status_does_not_convert_unknown_close_to_success(self):
        from services.applications import manual_auth_host as host
        for status, expected in (("CLOSE_UNCONFIRMED", 3), ("OPEN_UNAVAILABLE", 2),
                                 ("STALE", 2), ("unknown", 3)):
            with self.subTest(status=status), patch.object(host, "run_browser", return_value=status):
                self.assertEqual(host.run(io.BytesIO(json.dumps(self.payload).encode())), expected)
        with patch.object(host, "run_browser", side_effect=RuntimeError("private-value")):
            self.assertEqual(host.run(io.BytesIO(json.dumps(self.payload).encode())), 3)

    def test_budget_is_checked_before_browser_and_on_every_poll(self):
        from services.applications import manual_auth_host as host
        checks = []
        def browser(*args, check):
            checks.append(check())
            checks.append(check())
            return "MANUAL_AUTH_WINDOW_CLOSED"
        with patch.object(host, "monotonic", side_effect=[10, 10, 11, 41]), \
                patch.object(host, "run_browser", side_effect=browser):
            self.assertEqual(host.run(io.BytesIO(json.dumps(self.payload).encode())), 3)
        self.assertEqual(checks, [None])

    def test_read_is_bounded_and_entrypoint_silent(self):
        from services.applications import manual_auth_host as host
        class Input(io.BytesIO):
            def read(self, size=-1):
                self.assert_size = size
                return super().read(size)
        stream = Input(b"x" * 10000)
        self.assertEqual(host.run(stream), 2)
        self.assertEqual(stream.assert_size, 2049)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(host.sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(b"private-invalid")})()), \
                patch.object(host.sys, "stdout", out), patch.object(host.sys, "stderr", err), \
                patch.object(host.sys, "argv", ["host"]), patch.object(host.sys, "platform", "win32"):
            self.assertEqual(host.main(), 2)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
