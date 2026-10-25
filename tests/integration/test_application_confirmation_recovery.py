"""Receipt-only reconciliation safety, idempotence and privacy acceptance."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.applications.confirmation_recovery import ACTION, ConfirmationConflict, reconcile_confirmation
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ConfirmationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-confirmation-check-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate(); self.store.bind_workspace_mode("FIXTURE"); self.store.save_profile(dict(PROFILE))
        self.fixture = fixture_server(); self.origin = self.fixture.__enter__()
        discover(self.store, self.origin, "fixture")
        self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.store.transition(self.app_id, ["PREPARED"], "SUBMISSION_UNCONFIRMED")
        self.route_id = self.store.application(self.app_id)["route_id"]
        self.client = TestClient(create_app(self.store, self.origin, self.root,
                                           "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        self.path = f"/profiles/fixture/applications/{self.app_id}/confirmation-check"

    def tearDown(self):
        self.client.close(); self.fixture.__exit__(None, None, None)
        self.store.engine.dispose(); self.temp.cleanup()

    def check(self, request="check-1"):
        return reconcile_confirmation(self.store, "fixture", self.app_id, self.origin,
                                      request, operation_mode="FIXTURE")

    def events(self):
        with self.store.engine.connect() as connection:
            return list(connection.execute(select(m.audit.c.payload).where(m.audit.c.action == ACTION)).scalars())

    def test_missing_receipt_is_not_permission_to_retry_and_check_is_idempotent(self):
        with patch("services.applications.mock_adapter.sync_playwright", side_effect=AssertionError("No browser allowed")):
            result = self.check()
            replay = self.check()
        self.assertEqual(result["status"], "NO_VERIFIED_RECEIPT")
        self.assertFalse(result["provider_receipt_verified"])
        self.assertFalse(result["browser_started"])
        self.assertFalse(result["submission_attempted"])
        self.assertEqual(replay, {**result, "replayed": True})
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.store.application(self.app_id)["state"], "SUBMISSION_UNCONFIRMED")
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts", trust_env=False).json(), {"review": 0, "submit": 0})

    def test_parallel_identical_checks_have_one_durable_check_identity(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.check(), range(2)))
        self.assertEqual(len({row["check_id"] for row in results}), 1)
        self.assertEqual(sum(row["replayed"] for row in results), 1)
        self.assertEqual(len(self.events()), 1)

    def test_cross_profile_missing_and_nonpost_requests_are_rejected(self):
        with patch("services.applications.confirmation_recovery.MockClient", side_effect=AssertionError("No provider access")):
            self.assertEqual(self.client.post(self.path.replace("/fixture/", "/other/"), json={"request_id": "x"}).status_code, 404)
            self.assertEqual(self.client.post(self.path.replace(self.app_id, "missing"), json={"request_id": "x"}).status_code, 404)
            # The application SPA fallback rejects an unknown GET with 404.
            self.assertEqual(self.client.get(self.path).status_code, 404)
        self.assertFalse(self.events())

    def test_personal_mode_and_external_targets_fail_before_provider_access(self):
        with patch("services.applications.confirmation_recovery.MockClient", side_effect=AssertionError("No provider access")):
            with self.assertRaisesRegex(ConfirmationConflict, "FIXTURE_ONLY"):
                reconcile_confirmation(self.store, "fixture", self.app_id, self.origin, "x", operation_mode="PERSONAL")
            with self.store.engine.begin() as connection:
                connection.execute(update(m.applications).where(m.applications.c.id == self.app_id).values(target="REAL"))
            self.assertEqual(self.client.post(self.path, json={"request_id": "x"}).status_code, 409)
        self.assertFalse(self.events())

    def test_payload_cannot_authorize_a_submit_or_supply_an_external_origin(self):
        for payload in [{}, {"request_id": "x", "submit": True}, {"request_id": "x", "origin": "https://example.test"}, []]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.path, json=payload).status_code, 422)
        for request in ["", "x/y", "a" * 129, 1, None]:
            with self.subTest(request=request), self.assertRaises(ConfirmationConflict):
                self.check(request)
        self.assertFalse(self.events())

    def test_even_ancient_unfinished_attempt_is_not_stolen(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(id="legacy", application_id=self.app_id,
                state="RUNNING", steps=[], started_at="2000-01-01T00:00:00Z"))
        with patch("services.applications.confirmation_recovery.MockClient", side_effect=AssertionError("No provider access")):
            with self.assertRaisesRegex(ConfirmationConflict, "STILL_RESERVED"):
                self.check()
        self.assertFalse(self.events())

    def test_running_worker_is_not_stolen_even_with_expired_lease(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(id="worker", kind="APPLY_MOCK", entity_id=self.app_id,
                params={"submit": True}, state="RUNNING", updated_at=now(), lease_expires_at="2000-01-01T00:00:00Z"))
        with self.assertRaisesRegex(ConfirmationConflict, "STILL_RESERVED"):
            self.check()
        self.assertFalse(self.events())

    def test_route_changed_during_lookup_does_not_change_application(self):
        def changed(*args, **kwargs):
            with self.store.engine.begin() as connection:
                connection.execute(update(m.routes).where(m.routes.c.id == self.route_id).values(url=self.origin + "/changed"))
            return None
        with patch("services.applications.mock_adapter._receipt", side_effect=changed):
            with self.assertRaisesRegex(ConfirmationConflict, "ROUTE_CHANGED"):
                self.check()
        self.assertEqual(self.store.application(self.app_id)["state"], "SUBMISSION_UNCONFIRMED")
        self.assertFalse(self.events())

    def test_wrong_origin_is_rejected_without_contacting_it(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == self.route_id).values(url="https://example.test/apply"))
        with patch("services.applications.mock_adapter._receipt", side_effect=AssertionError("No provider request")):
            with self.assertRaisesRegex(ConfirmationConflict, "ORIGIN_MISMATCH"):
                self.check()
        self.assertFalse(self.events())

    def test_foreign_receipt_fails_existing_receipt_validation(self):
        response = Mock(); response.json.return_value = {"application_id": "someone-else", "receipt": "MOCK-forged"}
        with patch("services.discovery.pipeline.MockClient.get", return_value=response):
            result = self.check()
        self.assertEqual(result["status"], "NO_VERIFIED_RECEIPT")
        self.assertFalse(result["provider_receipt_verified"])
        self.assertIsNone(result["confirmation"])

    def test_later_outage_does_not_masquerade_as_fresh_verification_or_downgrade(self):
        receipt = {"reference_number": "MOCK-existing", "timestamp": now()}
        self.store.transition(self.app_id, ["SUBMISSION_UNCONFIRMED"], "CONFIRMED", confirmation=receipt)
        result = self.check()
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["confirmation_source"], "PREVIOUS_CONFIRMATION")
        self.assertFalse(result["provider_receipt_verified"])
        self.assertEqual(self.store.application(self.app_id)["confirmation"], receipt)
        self.assertEqual(self.store.application(self.app_id)["state"], "CONFIRMED")

    def test_receipt_only_api_hides_private_profile_and_filesystem_details(self):
        response = self.client.post(self.path, json={"request_id": "safe-read"})
        self.assertEqual(response.status_code, 200)
        for private in [PROFILE["email"], PROFILE["name"], str(self.root), self.origin]:
            self.assertNotIn(private, response.text)
        self.assertEqual(self.store.count("application_attempts"), 0)
