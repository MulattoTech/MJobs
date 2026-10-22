"""Public receipt evidence survives restart without permitting duplicate submission."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class FleetT10ConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-t10-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'fixture.db').as_posix()}"
        self.store = Store(self.url)
        self.addCleanup(lambda: self.store.engine.dispose())
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.fixture = fixture_server()
        self.origin = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        discover(self.store, self.origin, "fixture")
        self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.store.transition(self.app_id, ["PREPARED"], "SUBMISSION_UNCONFIRMED")
        self.path = f"/profiles/fixture/applications/{self.app_id}/confirmation-check"
        self.client = self._client()
        self.addCleanup(lambda: self.client.close())

    def _client(self, mode="FIXTURE"):
        return TestClient(create_app(self.store, self.origin, self.root,
                                    "redis://127.0.0.1:1/0", operation_mode=mode))

    def _restart(self):
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.client = self._client()

    def _refuses_duplicate(self):
        response = self.client.post(f"/profiles/fixture/applications/{self.app_id}/mock-run",
                                    json={"request_id": "duplicate-after-check", "submit": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.store.count("worker_operations"), 0)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_timeout_evidence_survives_lost_response_and_restart_without_retrying_submit(self):
        # Removing durable failure handling would return 503 and lose the saved evidence.
        with patch("services.applications.mock_adapter._receipt", side_effect=TimeoutError("private exception")):
            response = self.client.post(self.path, json={"request_id": "lost-response"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "CHECK_UNAVAILABLE")
        self.assertEqual(result["application_state"], "SUBMISSION_UNCONFIRMED")
        self.assertFalse(result["provider_receipt_verified"])
        self.assertIsNone(result["confirmation"])
        self.assertFalse(result["submission_attempted"])
        self.assertFalse(result["browser_started"])
        self._restart()
        with patch("services.applications.confirmation_recovery.MockClient", side_effect=AssertionError("readback/replay must not contact provider")):
            saved = self.client.get(self.path + "/latest")
            replay = self.client.post(self.path, json={"request_id": "lost-response"})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["result"]["check_id"], result["check_id"])
        self.assertEqual(saved.json()["result"]["status"], "CHECK_UNAVAILABLE")
        self.assertEqual(replay.json(), {**result, "replayed": True})
        self._refuses_duplicate()
        for private in ["private exception", PROFILE["email"], str(self.root), self.origin]:
            self.assertNotIn(private, saved.text)
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts", trust_env=False).json(), {"review": 0, "submit": 0})

    def test_missing_receipt_is_saved_and_readback_is_profile_scoped(self):
        self.assertEqual(self.client.get(self.path + "/latest").json(), {"result": None})
        result = self.client.post(self.path, json={"request_id": "missing"}).json()
        self.assertEqual(result["status"], "NO_VERIFIED_RECEIPT")
        self._restart()
        with patch("services.applications.confirmation_recovery.MockClient", side_effect=AssertionError("read only")):
            saved = self.client.get(self.path + "/latest")
            foreign = self.client.get(self.path.replace("/fixture/", "/other/") + "/latest")
        self.assertEqual(saved.json()["result"]["check_id"], result["check_id"])
        self.assertEqual(foreign.status_code, 404)
        self._refuses_duplicate()

    def test_readback_rejects_changed_route_and_personal_mode(self):
        self.client.post(self.path, json={"request_id": "route-before"})
        with self.store.engine.begin() as connection:
            route_id = self.store.application(self.app_id)["route_id"]
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(requisition="changed"))
        response = self.client.get(self.path + "/latest")
        self.assertEqual(response.status_code, 409)
        replay = self.client.post(self.path, json={"request_id": "route-before"})
        self.assertEqual(replay.status_code, 409)
        # A distinct empty synthetic workspace avoids changing the fixture
        # database's immutable operation-mode binding.
        personal = Store(f"sqlite:///{(self.root / 'synthetic-personal.db').as_posix()}")
        try:
            personal.migrate()
            with TestClient(create_app(personal, self.origin, self.root,
                                       "redis://127.0.0.1:1/0", operation_mode="PERSONAL")) as client:
                self.assertEqual(client.get(self.path + "/latest").status_code, 409)
        finally:
            personal.engine.dispose()

    def test_late_fixture_receipt_is_verified_once_and_saved_after_restart(self):
        # Set up a fictional receipt independently of the recovery consumer.
        with self.store.engine.connect() as connection:
            route = connection.execute(select(m.routes).where(
                m.routes.c.id == self.store.application(self.app_id)["route_id"])).mappings().one()
        response = httpx.post(self.origin + "/ats/submit/" + route["requisition"], data={
            "application_id": self.app_id, "answer": "Controlled fictional test application.",
            "confirmed": "yes"}, trust_env=False)
        self.assertEqual(response.status_code, 200)
        original_attempts = self.store.count("application_attempts")
        result = self.client.post(self.path, json={"request_id": "late-receipt"}).json()
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertTrue(result["provider_receipt_verified"])
        self.assertFalse(result["submission_attempted"])
        self._restart()
        with patch("services.applications.confirmation_recovery.MockClient", side_effect=AssertionError("saved evidence only")):
            saved = self.client.get(self.path + "/latest")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["result"]["confirmation"], result["confirmation"])
        self.assertEqual(self.store.application(self.app_id)["state"], "CONFIRMED")
        duplicate = self.client.post(f"/profiles/fixture/applications/{self.app_id}/mock-run",
                                     json={"request_id": "late-duplicate", "submit": True})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["code"], "APPLICATION_ALREADY_CONFIRMED")
        self.assertEqual(self.store.count("application_attempts"), original_attempts)
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts", trust_env=False).json(), {"review": 0, "submit": 1})

    def test_timeout_moves_submitting_to_explicit_unconfirmed_without_losing_prior_confirmation(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(m.applications.c.id == self.app_id).values(state="SUBMITTING"))
        with patch("services.applications.mock_adapter._receipt", side_effect=TimeoutError("synthetic")):
            response = self.client.post(self.path, json={"request_id": "submitting-timeout"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "CHECK_UNAVAILABLE")
        self.assertEqual(result["application_state"], "SUBMISSION_UNCONFIRMED")
        self.assertEqual(self.store.application(self.app_id)["state"], "SUBMISSION_UNCONFIRMED")
        receipt = {
            "receipt": "MOCK-earlier", "reference_number": "MOCK-earlier",
            "timestamp": "2026-10-17T04:00:00Z",
        }
        self.store.transition(self.app_id, ["SUBMISSION_UNCONFIRMED"], "CONFIRMED", confirmation=receipt)
        with patch("services.applications.mock_adapter._receipt", side_effect=TimeoutError("synthetic")):
            result = self.client.post(self.path, json={"request_id": "confirmed-timeout"}).json()
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["confirmation_source"], "PREVIOUS_CONFIRMATION")
        self.assertFalse(result["provider_receipt_verified"])
        self.assertEqual(result["confirmation"], {
            key: receipt[key] for key in ("reference_number", "timestamp")
        })
        self.assertEqual(self.store.application(self.app_id)["confirmation"], receipt)
        self.assertEqual(self.store.application(self.app_id)["state"], "CONFIRMED")
