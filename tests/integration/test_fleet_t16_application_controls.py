"""Connected cancellation, explicit retry and durable audit controls (T16)."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import controlled_application_dry_run as controls_api
from apps.api.controlled_application_dry_run import create_controlled_application_dry_run_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.rehearsal_store import RehearsalStoreError
from tests.integration.test_spine import PROFILE
from apps.api.controlled_application_dry_run import ControlsRequest
from pydantic import ValidationError


class T16ApplicationControlsTests(unittest.TestCase):
    def _new_store(self):
        self.database_url = f"sqlite:///{(self.root / 'form-review.db').as_posix()}"
        store = Store(self.database_url)
        store.migrate()
        return store

    def setUp(self):
        from tests.integration.test_controlled_application_dry_run import ControlledApplicationDryRunTests
        profile = deepcopy(PROFILE)
        profile["answers"].append({
            "id": "weekends", "question": "Can you work weekends?",
            "text": "Yes, weekends are available.", "verified": True,
            "user_approved": True, "confidence": 100,
            "source": "fictional-test-profile",
        })
        with patch("tests.integration.test_application_form_review_flow.PROFILE", profile):
            ControlledApplicationDryRunTests.setUp(self)

    def path(self):
        return f"/profiles/fixture/applications/{self.application_id}/controlled-dry-run"

    def review_and_queue(self, request_id):
        from tests.integration.test_controlled_application_dry_run import ControlledApplicationDryRunTests
        return ControlledApplicationDryRunTests.review_and_queue(self, request_id)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_controls_request_is_strict_and_bounded(self):
        request = ControlsRequest.model_validate({
            "request_id": "t16-controls", "paused": True, "daily_limit": 100,
        })
        self.assertEqual(request.daily_limit, 100)
        for invalid in (
            {"request_id": "t16-controls", "paused": "true", "daily_limit": 10},
            {"request_id": "t16-controls", "paused": True, "daily_limit": True},
            {"request_id": "t16-controls", "paused": True, "daily_limit": -1},
            {"request_id": "t16-controls", "paused": True, "daily_limit": 101},
            {"request_id": "t16-controls", "paused": True, "daily_limit": 10,
             "submission_enabled": True},
            {"request_id": "t16-controls", "paused": True, "daily_limit": 10,
             "password": "PRIVATE_SENTINEL"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ControlsRequest.model_validate(invalid)

    def test_controls_routes_validate_scope_and_delegate_fixed_contract(self):
        controls_path = self.path() + "/controls"
        for payload in (
            {"request_id": "t16-controls", "paused": "true", "daily_limit": 10},
            {"request_id": "t16-controls", "paused": True, "daily_limit": True},
            {"request_id": "t16-controls", "paused": True, "daily_limit": -1},
            {"request_id": "t16-controls", "paused": True, "daily_limit": 101},
            {"request_id": "t16-controls", "paused": True, "daily_limit": 10,
             "submission_enabled": True},
            {"request_id": "t16-controls", "paused": True, "daily_limit": 10,
             "password": "PRIVATE_SENTINEL"},
        ):
            response = self.client.put(controls_path, json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("PRIVATE_SENTINEL", response.text)

        controls = {
            "profile_id": "fixture", "application_id": self.application_id,
            "request_id": "t16-controls", "paused": True, "daily_limit": 2,
            "submission_enabled": False,
        }
        with patch.object(controls_api.rehearsal_store, "set_controlled_controls",
                          return_value={"controls": controls, "replayed": False},
                          create=True) as setter:
            updated = self.client.put(controls_path, json={
                "request_id": "t16-controls", "paused": True, "daily_limit": 2,
            })
        self.assertEqual(updated.status_code, 200, updated.text)
        setter.assert_called_once_with(
            self.store, "fixture", self.application_id,
            request_id="t16-controls", paused=True, daily_limit=2,
        )

        with patch.object(controls_api.rehearsal_store, "get_controlled_controls",
                          return_value=controls, create=True) as getter:
            saved = self.client.get(controls_path)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), controls)
        getter.assert_called_once_with(self.store, "fixture", self.application_id)

        other = controls_path.replace("/profiles/fixture/", "/profiles/other/")
        with patch.object(controls_api.rehearsal_store, "get_controlled_controls",
                          side_effect=RehearsalStoreError("APPLICATION_NOT_FOUND"),
                          create=True):
            self.assertEqual(self.client.get(other).status_code, 404)
        with patch.object(controls_api.rehearsal_store, "set_controlled_controls",
                          side_effect=RehearsalStoreError("APPLICATION_NOT_FOUND"),
                          create=True):
            self.assertEqual(self.client.put(other, json={
                "request_id": "t16-controls", "paused": True, "daily_limit": 2,
            }).status_code, 404)

    def test_cancel_then_explicit_retry_persists_safe_state_across_reload(self):
        _, body, queued = self.review_and_queue("t16-interrupted-run")
        cancel_path = self.path() + "/runs/" + body["request_id"] + "/cancel"
        cancelled = self.client.post(cancel_path)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["state"], "CANCELLED")

        interrupted = self.client.get(self.path() + "/runs/" + body["request_id"])
        self.assertEqual(interrupted.status_code, 200, interrupted.text)
        self.assertTrue(interrupted.json()["cancel_requested"])
        self.assertIsNone(interrupted.json()["receipt"])

        retry_path = self.path() + "/runs/" + body["request_id"] + "/retry"
        missing_consent = self.client.post(retry_path, json={
            "request_id": "t16-retry-without-consent",
            "fingerprint": body["fingerprint"],
        })
        self.assertEqual(missing_consent.status_code, 422)
        retry = self.client.post(retry_path, json={
            "request_id": "t16-explicit-retry",
            "fingerprint": body["fingerprint"],
            "acknowledged": True,
        })
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(retry.json()["state"], "QUEUED")

        read_path = self.path() + "/runs/t16-explicit-retry"
        before_reload = self.client.get(read_path)
        self.assertEqual(before_reload.status_code, 200, before_reload.text)
        self.assertEqual(before_reload.json()["state"], "QUEUED")
        self.assertIsNone(before_reload.json()["receipt"])
        self.assertEqual(before_reload.json()["retry_of"], body["request_id"])
        self.assertFalse(self.client.get(self.path()).json()["submission_enabled"])

        with self.store.engine.connect() as connection:
            actions = set(connection.execute(select(m.audit.c.action).where(
                m.audit.c.entity_id == queued["operation_id"],
            )).scalars())
        self.assertIn("CANCELLATION_REQUESTED", actions)
        self.assertNotIn("APPLICATION_DRY_RUN", actions)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        app = FastAPI()
        app.include_router(create_controlled_application_dry_run_router(self.store))
        self.client = TestClient(app)
        after_reload = self.client.get(read_path)
        self.assertEqual(after_reload.status_code, 200, after_reload.text)
        self.assertEqual(after_reload.json(), before_reload.json())
        self.assertIsNone(after_reload.json()["receipt"])
        self.assertFalse(self.client.get(self.path()).json()["submission_enabled"])


if __name__ == "__main__":
    unittest.main()
