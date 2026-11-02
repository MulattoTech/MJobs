"""Manual-auth browser profiles must be direct, private directories."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.applications import manual_auth
from services.applications.browser_profiles import browser_profile_path


APPLICATION_ID = "a" * 64
ATTEMPT_ID = "b" * 32


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class _Connection:
    def __init__(self, rows):
        self.rows = iter(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _statement):
        return _Rows(next(self.rows))


class _Engine:
    def __init__(self, rows):
        self.rows = rows

    def connect(self):
        return _Connection(self.rows)


class _Store:
    def __init__(self, application_id=APPLICATION_ID, attempt_id=ATTEMPT_ID):
        self.application_id = application_id
        route = {
            "provider": "mock-ats",
            "url": "http://127.0.0.1:8123/ats/apply/job_1",
            "requisition": "job_1",
        }
        attempt = {
            "id": attempt_id,
            "state": "AUTH_REQUIRED",
            "steps": ["NAVIGATE:COMPLETED"],
        }
        self.engine = _Engine([route, attempt, attempt])

    def workspace_status(self):
        return {"state": "ACTIVE", "operation_mode": "FIXTURE"}

    def application(self, application_id):
        if application_id != self.application_id:
            raise LookupError
        return {"route_id": "route1", "target": "MOCK", "state": "AUTH_REQUIRED"}


class ManualAuthPathSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-manual-auth-path-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.application_root = self.root / APPLICATION_ID
        self.application_root.mkdir()
        self.profile = self.application_root / "browser-profile"

    def _open(self, application_id=APPLICATION_ID, attempt_id=ATTEMPT_ID):
        return manual_auth.open_manual_auth_window(
            _Store(application_id, attempt_id), application_id, attempt_id,
            "http://127.0.0.1:8123", self.root,
        )

    def _directory_alias(self, target):
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(self.profile))
        else:
            self.profile.symlink_to(target, target_is_directory=True)
        self.addCleanup(self.profile.rmdir if os.name == "nt" else self.profile.unlink)

    def test_redirected_profile_fails_closed_before_browser_launch(self):
        redirected = self.application_root / "redirected-private-profile"
        redirected.mkdir()
        sentinel = redirected / "synthetic-private-cookie"
        sentinel.write_text("not-a-real-secret", encoding="utf-8")
        self._directory_alias(redirected)

        with patch.object(
            manual_auth, "run_browser", return_value="BROWSER_STARTED",
        ) as run_browser:
            result = self._open()

        self.assertEqual(result, manual_auth.STALE)
        run_browser.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "not-a-real-secret")
        self.assertNotIn(str(redirected), result)

    def test_direct_profile_preserves_fixture_manual_auth_flow(self):
        self.profile.mkdir()

        with patch.object(
            manual_auth, "run_browser", return_value=manual_auth.MANUAL_AUTH_WINDOW_CLOSED,
        ) as run_browser, patch.dict(os.environ, {"MJOBS_BROWSER_DEBUG_CAPTURE": "1"}):
            result = self._open()

        self.assertEqual(result, manual_auth.MANUAL_AUTH_WINDOW_CLOSED)
        self.assertTrue(run_browser.call_args.kwargs["debug_capture"])
        self.assertEqual(run_browser.call_args.kwargs["application_root"], self.application_root)
        self.assertEqual(manual_auth._diagnostic_capture_root(
            run_browser.call_args.args[0], run_browser.call_args.kwargs["application_root"]),
            self.application_root / "debug-captures")

    def test_diagnostic_root_is_recontained_after_attempt_resolution(self):
        self.profile.mkdir()
        resolve_attempt = manual_auth._resolve_auth_attempt
        realpath = os.path.realpath
        for destination in (
            self.root,
            self.root.with_name(self.root.name + "-outside") / APPLICATION_ID,
            self.root.parent / "outside-output" / APPLICATION_ID,
        ):
            with self.subTest(destination=destination):
                attempt_resolved = False

                def resolve_then_redirect(*args, **kwargs):
                    nonlocal attempt_resolved
                    result = resolve_attempt(*args, **kwargs)
                    self.assertIsNotNone(result)
                    attempt_resolved = True
                    return result

                def redirected_realpath(path, *args, **kwargs):
                    # Model a changed canonical destination only after the
                    # real persisted-attempt/session validation has succeeded.
                    if attempt_resolved and Path(path) == self.application_root:
                        return str(destination)
                    return realpath(path, *args, **kwargs)

                with patch.object(manual_auth, "_resolve_auth_attempt", side_effect=resolve_then_redirect), \
                        patch.object(manual_auth.os.path, "realpath", side_effect=redirected_realpath), \
                        patch.dict(os.environ, {"MJOBS_BROWSER_DEBUG_CAPTURE": "1"}), \
                        patch.object(manual_auth, "run_browser", return_value="BROWSER_STARTED") as run_browser:
                    result = self._open()
                self.assertEqual(result, manual_auth.STALE)
                run_browser.assert_not_called()
        self.assertFalse(list(self.root.rglob("debug-captures")))

    def test_named_profile_diagnostics_keep_the_validated_application_root(self):
        for label in ("review", "browser-profile"):
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                application_root = root / APPLICATION_ID
                with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": label}):
                    selected = browser_profile_path(application_root, initialize=True)
                # A later environment change must retain the pinned session,
                # including a name identical to the browser-profile directory.
                with patch.dict(os.environ, {
                    "MJOBS_BROWSER_PROFILE": "different", "MJOBS_BROWSER_DEBUG_CAPTURE": "1",
                }), patch.object(manual_auth, "run_browser",
                                 return_value=manual_auth.MANUAL_AUTH_WINDOW_CLOSED) as run_browser:
                    result = manual_auth.open_manual_auth_window(
                        _Store(), APPLICATION_ID, ATTEMPT_ID,
                        "http://127.0.0.1:8123", root,
                    )
                self.assertEqual(result, manual_auth.MANUAL_AUTH_WINDOW_CLOSED)
                self.assertEqual(run_browser.call_args.args[0], selected)
                self.assertEqual(run_browser.call_args.kwargs["application_root"], application_root)
                self.assertTrue(run_browser.call_args.kwargs["debug_capture"])
                self.assertEqual(manual_auth._diagnostic_capture_root(
                    selected, run_browser.call_args.kwargs["application_root"]),
                    application_root / "debug-captures")

    def test_persisted_path_shaped_application_id_fails_closed(self):
        self.profile.mkdir()
        (self.root / "nested" / APPLICATION_ID / "browser-profile").mkdir(parents=True)
        for application_id in (
            "nested/" + APPLICATION_ID, "../" + APPLICATION_ID, str(self.application_root),
            "..\\" + APPLICATION_ID, "C:\\" + APPLICATION_ID,
            "\\\\server\\share\\" + APPLICATION_ID, "%2e%2e%2f" + APPLICATION_ID,
            APPLICATION_ID + "\n", APPLICATION_ID + "\0", "A" * 64, None, 64,
        ):
            with self.subTest(application_id=application_id), patch.dict(
                os.environ, {"MJOBS_BROWSER_DEBUG_CAPTURE": "1"},
            ), patch.object(manual_auth, "run_browser", return_value="BROWSER_STARTED") as run_browser:
                result = self._open(application_id=application_id)
            self.assertEqual(result, manual_auth.STALE)
            run_browser.assert_not_called()
        self.assertFalse(list(self.root.rglob("debug-captures")))


if __name__ == "__main__":
    unittest.main()
