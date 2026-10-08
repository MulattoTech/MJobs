"""The mounted manual-auth API is profile-scoped and side-effect bounded."""
from pathlib import Path
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


class ManualAuthRouterMountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-manual-auth-mount-")
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
        self.url = f"/profiles/fixture/applications/{self.application_id}/manual-auth"
        with self.store.engine.begin() as connection:
            route_id = self.store.application(self.application_id)["route_id"]
            route = connection.execute(select(m.routes).where(
                m.routes.c.id == route_id)).mappings().one()
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                url=f"{self.origin}/ats/apply/{route['requisition']}?auth_expired=true"))
        paused = apply_mock(self.store, self.application_id, self.origin, self.root,
                            submit=True)
        self.assertEqual(paused["state"], "AUTH_REQUIRED")

        self.client = TestClient(create_app(
            self.store, self.origin, self.root, operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_mounted_mock_auth_route_opens_same_attempt_without_retry_or_submission(self):
        before = self.store.application(self.application_id)
        attempts_before = self.store.count("application_attempts")
        operations_before = self.store.count("worker_operations")
        with self.store.engine.connect() as connection:
            attempt = connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == self.application_id
            ).order_by(m.attempts.c.started_at.desc(), m.attempts.c.id.desc())).mappings().one()

        with patch("apps.api.manual_auth.open_mock_authentication",
                   return_value="MANUAL_AUTH_WINDOW_CLOSED") as open_window:
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "status": "MANUAL_AUTH_WINDOW_CLOSED",
            "application_id": self.application_id,
            "attempt_id": attempt["id"],
        })
        open_window.assert_called_once()
        self.assertEqual(open_window.call_args.kwargs["application_id"], self.application_id)
        self.assertEqual(open_window.call_args.kwargs["attempt_id"], attempt["id"])
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.application(self.application_id)["trace"], before["trace"])
        self.assertEqual(self.store.count("application_attempts"), attempts_before)
        self.assertEqual(self.store.count("worker_operations"), operations_before)
        self.assertIsNone(self.store.application(self.application_id)["confirmation"])
        self.assertEqual(__import__("httpx").get(
            self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})

    def test_manual_auth_rejects_other_profile_and_non_mock_application(self):
        with patch("apps.api.manual_auth.open_mock_authentication") as open_window:
            wrong_profile = self.client.post(
                f"/profiles/other/applications/{self.application_id}/manual-auth")
            with self.store.engine.begin() as connection:
                connection.execute(update(m.applications).where(
                    m.applications.c.id == self.application_id).values(target="EXTERNAL"))
            non_mock = self.client.post(self.url)

        self.assertEqual(wrong_profile.status_code, 404, wrong_profile.text)
        self.assertEqual(non_mock.status_code, 409, non_mock.text)
        open_window.assert_not_called()
        self.assertEqual(self.store.count("application_attempts"), 1)
        self.assertEqual(__import__("httpx").get(
            self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})

    def test_manual_auth_rejects_stale_attempt_and_non_fixture_runtime(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.attempts).where(
                m.attempts.c.application_id == self.application_id).values(state="COMPLETED"))
        with patch("apps.api.manual_auth.open_mock_authentication") as open_window:
            stale = self.client.post(self.url)

        personal_store = Store(f"sqlite:///{(self.root / 'personal.db').as_posix()}")
        personal_store.migrate()
        personal_store.bind_workspace_mode("PERSONAL")
        personal = TestClient(create_app(
            personal_store, self.origin, self.root, operation_mode="PERSONAL"))
        try:
            non_fixture = personal.post(self.url)
        finally:
            personal.close()
            personal_store.engine.dispose()

        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(non_fixture.status_code, 409, non_fixture.text)
        open_window.assert_not_called()
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(self.store.count("application_attempts"), 1)
        self.assertEqual(__import__("httpx").get(
            self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})

    def test_manual_auth_rejects_non_auth_required_application(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id).values(state="PREPARED"))
        with patch("apps.api.manual_auth.open_mock_authentication") as open_window:
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, 409, response.text)
        open_window.assert_not_called()
        self.assertEqual(self.store.count("application_attempts"), 1)
        self.assertEqual(__import__("httpx").get(
            self.origin + "/_test/request-counts").json(), {"review": 0, "submit": 0})
