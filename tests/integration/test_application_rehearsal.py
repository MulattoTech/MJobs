"""Connected local-only rehearsal API backed by audit receipts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.application_rehearsal import create_application_rehearsal_router
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


GREENHOUSE_URL = "https://boards.greenhouse.io/example/jobs/1234567"
PRIVATE_SENTINELS = (
    "PRIVATE_REHEARSAL_VALUE", "PRIVATE_REHEARSAL_PASSWORD",
    "PRIVATE_REHEARSAL_SESSION", "PRIVATE_REHEARSAL_COOKIE",
)
_REVIEW_ACTION = "APPLICATION_FORM_REHEARSAL_REVIEWED"


class ApplicationRehearsalApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-rehearsal-api-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'rehearsal.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        app = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == app["route_id"],
            ).values(provider="greenhouse", tenant="example", url=GREENHOUSE_URL))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
        self._seed_form_analysis()
        self.client = self._client(self.store)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _client(self, store):
        app = FastAPI()
        app.include_router(create_application_rehearsal_router(store))
        return TestClient(app)

    def _path(self, profile_id="fixture", application_id=None):
        application_id = application_id or self.application_id
        return f"/profiles/{profile_id}/applications/{application_id}/rehearsal"

    def _seed_form_analysis(self):
        app = self.store.application(self.application_id)
        analysis = {
            "status": "ANALYZED",
            "reason": None,
            "fields": [
                {"id": "email", "name": "email", "type": "email",
                 "required": True, "review_reason": None},
                {"id": "custom_question", "name": "custom_question", "type": "text",
                 "required": True, "review_reason": "AMBIGUOUS_FIELD"},
            ],
        }
        payload = {
            "operation_id": "synthetic-form-review-operation",
            "request_id": "synthetic-form-review-request",
            "input_fingerprint": "a" * 64,
            "profile_id": "fixture",
            "route_id": app["route_id"],
            "provider": "greenhouse",
            "analysis": analysis,
        }
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id=identity("synthetic-form-review", self.application_id),
                entity_id=self.application_id,
                action="APPLICATION_FORM_REVIEW",
                payload=payload,
                timestamp=now(),
            ))

    def _fingerprint(self):
        response = self.client.get(self._path())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["fingerprint"], response.json()

    def _review(self, request_id="rehearsal-review-1", fingerprint=None, **overrides):
        fingerprint = fingerprint or self._fingerprint()[0]
        payload = {"request_id": request_id, "fingerprint": fingerprint,
                   "acknowledged": True}
        payload.update(overrides)
        return self.client.post(self._path() + "/reviews", json=payload)

    def _review_events(self):
        with self.store.engine.connect() as connection:
            return list(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == _REVIEW_ACTION,
            )).mappings())

    def test_get_returns_current_preview_and_no_latest_receipt_initially(self):
        response = self.client.get(self._path())

        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["profile_id"], "fixture")
        self.assertEqual(preview["application_id"], self.application_id)
        self.assertEqual(preview["route_id"], self.store.application(self.application_id)["route_id"])
        self.assertFalse(preview["submission_enabled"])
        self.assertIsNone(preview["latest_review"])
        email = next(field for field in preview["fields"] if field["key"] == "email")
        self.assertEqual(email["source"], "CONTACT")
        self.assertEqual(email["value"], PROFILE["email"])
        unresolved = next(field for field in preview["fields"]
                          if field["key"] == "custom_question")
        self.assertEqual(unresolved["source"], "UNRESOLVED")
        self.assertIsNone(unresolved["value"])
        self.assertFalse(self._review_events())
        for sentinel in PRIVATE_SENTINELS:
            self.assertNotIn(sentinel, response.text)

    def test_acknowledgement_persists_minimal_idempotent_receipt_and_reloads(self):
        fingerprint, preview = self._fingerprint()
        application_before = self.store.application(self.application_id)

        first = self._review("review-once", fingerprint)
        self.assertEqual(first.status_code, 200, first.text)
        receipt = first.json()
        self.assertEqual(receipt["profile_id"], "fixture")
        self.assertEqual(receipt["application_id"], self.application_id)
        self.assertEqual(receipt["request_id"], "review-once")
        self.assertEqual(receipt["fingerprint"], fingerprint)
        self.assertEqual(receipt["profile_version"], preview["profile_version"])
        self.assertTrue(receipt["acknowledged"])
        self.assertFalse(receipt["replayed"])

        replay = self._review("review-once", fingerprint)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(len(self._review_events()), 1)
        self.assertEqual(self.store.application(self.application_id), application_before)

        stored = self._review_events()[0]["payload"]
        self.assertEqual(set(stored), {
            "receipt_version", "profile_id", "application_id", "request_id", "fingerprint",
            "profile_version", "route_id", "acknowledged",
        })
        self.assertEqual(stored["receipt_version"], 1)
        self.assertEqual(stored["fingerprint"], fingerprint)
        self.assertTrue(stored["acknowledged"])
        for sentinel in PRIVATE_SENTINELS:
            self.assertNotIn(sentinel, str(stored))

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client(self.store)
        latest = self.client.get(self._path())
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["latest_review"]["request_id"], "review-once")
        self.assertEqual(latest.json()["latest_review"]["fingerprint"], fingerprint)
        self.assertTrue(latest.json()["latest_review"]["acknowledged"])
        self.assertFalse(latest.json()["submission_enabled"])

    def test_profile_or_route_change_makes_review_stale_without_writing_receipt(self):
        fingerprint, _ = self._fingerprint()
        changed = deepcopy(PROFILE)
        changed["email"] = "updated@example.test"
        self.store.save_profile(changed)

        response = self._review("stale-after-profile-change", fingerprint)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "APPLICATION_REHEARSAL_STALE")
        self.assertEqual(self._review_events(), [])

    def test_cross_profile_unknown_app_and_missing_form_review_fail_closed(self):
        denied = self.client.get(self._path("another-profile"))
        self.assertEqual(denied.status_code, 404)
        missing = self.client.get(self._path(application_id="missing-application"))
        self.assertEqual(missing.status_code, 404)

        with self.store.engine.begin() as connection:
            connection.execute(m.audit.delete().where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            ))
        no_review = self.client.get(self._path())
        self.assertEqual(no_review.status_code, 409, no_review.text)
        self.assertEqual(no_review.json()["detail"]["code"], "APPLICATION_REHEARSAL_FORM_REVIEW_REQUIRED")

    def test_invalid_acknowledgement_body_is_sanitized_and_not_persisted(self):
        fingerprint, _ = self._fingerprint()
        response = self._review(
            "invalid-ack", fingerprint, acknowledged=False,
            credentials={"password": "PRIVATE_REHEARSAL_PASSWORD"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "APPLICATION_REHEARSAL_INVALID_REQUEST")
        self.assertNotIn("PRIVATE_REHEARSAL_PASSWORD", response.text)
        self.assertEqual(self._review_events(), [])


if __name__ == "__main__":
    unittest.main()
