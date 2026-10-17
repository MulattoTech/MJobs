"""Connected cancellation, explicit retry and durable audit controls (T16)."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.controlled_application_dry_run import create_controlled_application_dry_run_router
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


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
