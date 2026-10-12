"""Explicit MOCK sign-in opens the saved session without resuming an attempt."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ManualAuthSessionApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-manual-auth-api-")
        self.root = Path(self.directory.name)
        self.database = self.root / "manual-auth.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile"})
        self.fixture = fixture_server()
        self.origin = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        discover(self.store, self.origin, "fixture")
        job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        with self.store.engine.begin() as connection:
            route_id = self.store.application(self.application_id)["route_id"]
            route = connection.execute(select(m.routes).where(
                m.routes.c.id == route_id)).mappings().one()
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                url=f"{self.origin}/ats/apply/{route['requisition']}?auth_expired=true"))
        paused = apply_mock(self.store, self.application_id, self.origin, self.root,
                            submit=True)
        self.assertEqual(paused["state"], "AUTH_REQUIRED")
        self.client = TestClient(create_app(self.store, self.origin, self.root,
                                            operation_mode="FIXTURE"))
        self.url = (f"/profiles/fixture/applications/{self.application_id}"
                    "/manual-auth")

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_user_started_sign_in_opens_same_attempt_profile_without_retry_or_submission(self):
        before = self.store.application(self.application_id)
        before_attempts = self.store.count("application_attempts")
        with self.store.engine.connect() as connection:
            attempt = connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == self.application_id
            ).order_by(m.attempts.c.started_at.desc())).mappings().first()
        attempt_id = attempt["id"]
        with patch("apps.api.manual_auth.open_mock_authentication",
                   return_value="MANUAL_AUTH_WINDOW_CLOSED") as open_window:
            response = self.client.post(self.url, json={})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "status": "MANUAL_AUTH_WINDOW_CLOSED",
            "application_id": self.application_id,
            "attempt_id": attempt_id,
        })
        open_window.assert_called_once()
        self.assertEqual(open_window.call_args.kwargs["application_id"], self.application_id)
        self.assertEqual(open_window.call_args.kwargs["attempt_id"], attempt_id)
        self.assertEqual(open_window.call_args.kwargs["mock_origin"], self.origin)
        self.assertEqual(Path(open_window.call_args.kwargs["output_dir"]), self.root)
        self.assertEqual(self.store.count("application_attempts"), before_attempts)
        after = self.store.application(self.application_id)
        self.assertEqual(after["state"], "AUTH_REQUIRED")
        self.assertEqual(after["trace"], before["trace"])
        self.assertIsNone(after["confirmation"])
        self.assertNotIn("mock_session", json.dumps(response.json()))
        self.assertNotIn("fixture-password", response.text)
        self.assertNotIn("set-cookie", {key.lower() for key in response.headers})
        counts = __import__("httpx").get(self.origin + "/_test/request-counts").json()
        self.assertEqual(counts, {"review": 0, "submit": 0})

    def test_unsupported_runtime_and_other_profile_fail_closed_without_opening_session(self):
        personal_store = Store(f"sqlite:///{(self.root / 'personal.db').as_posix()}")
        personal_store.migrate()
        personal_store.bind_workspace_mode("PERSONAL")
        personal = TestClient(create_app(personal_store, self.origin, self.root,
                                         operation_mode="PERSONAL"))
        try:
            with patch("apps.api.manual_auth.open_mock_authentication") as open_window:
                unsupported = personal.post(self.url, json={})
                foreign = self.client.post(
                    f"/profiles/other/applications/{self.application_id}/manual-auth", json={})
            self.assertEqual(unsupported.status_code, 409, unsupported.text)
            self.assertEqual(foreign.status_code, 404, foreign.text)
            open_window.assert_not_called()
        finally:
            personal.close()
            personal_store.engine.dispose()

    def test_unsupported_headed_browser_runtime_fails_closed_without_attempt_or_submission(self):
        before_attempts = self.store.count("application_attempts")
        with patch("apps.api.manual_auth.open_mock_authentication",
                   return_value="OPEN_UNAVAILABLE") as open_window:
            response = self.client.post(self.url, json={})

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {
            "detail": "Manual sign-in window is unavailable on this runtime"})
        open_window.assert_called_once()
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.count("application_attempts"), before_attempts)
        self.assertNotIn("mock_session", response.text)
        counts = __import__("httpx").get(self.origin + "/_test/request-counts").json()
        self.assertEqual(counts, {"review": 0, "submit": 0})

    def test_browser_callback_exposes_original_api_deadline(self):
        from workers.operations import OperationTimedOut
        now = [10.0]
        observed = []
        def open_window(**kwargs):
            check = kwargs['check']
            self.assertTrue(callable(getattr(check, 'remaining_seconds', None)),
                            'Browser callback must expose its original remaining budget')
            observed.append(check.remaining_seconds())
            now[0] = 10.75
            observed.append(check.remaining_seconds())
            now[0] = 11.0
            with self.assertRaises(OperationTimedOut):
                check.remaining_seconds()
            return 'MANUAL_AUTH_WINDOW_CLOSED'
        with patch('apps.api.manual_auth.monotonic', side_effect=lambda: now[0]), \
                patch('apps.api.manual_auth.timeout', return_value=1), \
                patch('apps.api.manual_auth.open_mock_authentication', side_effect=open_window):
            response = self.client.post(self.url, json={})
        self.assertEqual(observed, [1.0, .25])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.application(self.application_id)['state'], 'AUTH_REQUIRED')

    def test_cancellation_query_time_is_subtracted_from_browser_budget(self):
        now = [10.0]
        observed = []
        def cancellation_check(*args, **kwargs):
            now[0] += .4
        def open_window(**kwargs):
            observed.append(kwargs['check'].remaining_seconds())
            return 'MANUAL_AUTH_WINDOW_CLOSED'
        with patch('apps.api.manual_auth.monotonic', side_effect=lambda: now[0]), \
                patch('apps.api.manual_auth.timeout', return_value=1), \
                patch('apps.api.manual_auth.check_cancelled', side_effect=cancellation_check), \
                patch('apps.api.manual_auth.open_mock_authentication', side_effect=open_window):
            response = self.client.post(self.url, json={})
        self.assertEqual(len(observed), 1)
        self.assertAlmostEqual(observed[0], .2)
        self.assertEqual(response.status_code, 409, response.text)
