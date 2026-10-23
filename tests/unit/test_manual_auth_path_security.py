"""Manual-auth browser profiles must be direct, private directories."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.applications import manual_auth


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

        with patch.object(manual_auth, "run_browser", return_value="BROWSER_STARTED"):
            result = self._open()

        self.assertEqual(result, manual_auth.STALE)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "not-a-real-secret")
        self.assertNotIn(str(redirected), result)

    def test_direct_profile_preserves_fixture_manual_auth_flow(self):
        self.profile.mkdir()

        with patch.object(
            manual_auth, "run_browser", return_value=manual_auth.MANUAL_AUTH_WINDOW_CLOSED,
        ):
            result = self._open()

        self.assertEqual(result, manual_auth.MANUAL_AUTH_WINDOW_CLOSED)

    def test_persisted_path_shaped_application_id_fails_closed(self):
        application_id = "nested/application"
        profile = self.root / application_id / "browser-profile"
        profile.mkdir(parents=True)

        with patch.object(manual_auth, "run_browser", return_value="BROWSER_STARTED"):
            result = self._open(application_id=application_id)

        self.assertEqual(result, manual_auth.STALE)
        self.assertNotIn(str(profile), result)


if __name__ == "__main__":
    unittest.main()
