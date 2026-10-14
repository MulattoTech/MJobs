"""Durable, profile-scoped review and application of user-reported email signals."""
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.email_status import create_email_status_router
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.documents.generate import prepare
from services.audit_history import list_audit_history
from services.discovery.pipeline import discover
from services.tracking.applications import read_application_tracking, set_application_pipeline_status
from tests.integration.test_spine import PROFILE


class EmailStatusApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-email-status-")
        self.root = Path(self.directory.name)
        self.database = self.root / "email-status.db"
        self.url = f"sqlite:///{self.database.as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.foreign_application_id = prepare(self.store, job["id"], "other", self.root)
        self.app = FastAPI()
        self.app.include_router(create_email_status_router(self.store))
        self.client = TestClient(self.app)
        self.base = f"/profiles/fixture/applications/{self.application_id}/email-status"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _preview(self, content, format="text", *, base=None):
        response = self.client.post((base or self.base) + "/preview", json={
            "content": content,
            "format": format,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _audit(self, action):
        with self.store.engine.connect() as connection:
            return list(connection.execute(select(m.audit).where(
                m.audit.c.action == action)).mappings())

    def test_preview_persists_only_safe_metadata_and_deduplicates_at_tracking_sequence(self):
        private_subject = "PRIVATE_EMAIL_SUBJECT_SENTINEL"
        private_address = "private.sender@example.test"
        private_body = "PRIVATE_EMAIL_BODY_SENTINEL"
        content = (
            f"From: {private_address}\nSubject: {private_subject}\n\n"
            "Thank you for applying. We have received your application.\n" + private_body
        )
        preview = self._preview(content)
        self.assertEqual(preview["category"], "CONFIRMATION")
        self.assertEqual(preview["suggested_status"], "APPLIED")
        self.assertEqual(preview["expected_tracking_sequence"], 0)
        self.assertEqual(preview["attribution"], "USER_REPORTED_EMAIL")
        serialized = str(preview)
        for private_value in (content, private_subject, private_address, private_body):
            self.assertNotIn(private_value, serialized)

        replay = self._preview(content)
        self.assertEqual(replay, preview)
        receipts = self._audit("EMAIL_STATUS_PREVIEW")
        self.assertEqual(len(receipts), 1)
        payload = receipts[0]["payload"]
        self.assertEqual(payload["content_sha256"], preview["content_sha256"])
        self.assertEqual(payload["expected_tracking_sequence"], 0)
        self.assertNotIn("content", payload)
        self.assertNotIn("subject", payload)
        self.assertNotIn("sender", payload)
        self.assertNotIn(private_subject, str(payload))
        self.assertNotIn(private_address, str(payload))
        self.assertNotIn(private_body, str(payload))

        # A later tracking event makes a repeat import a new, current preview.
        set_application_pipeline_status(
            self.store, "fixture", self.application_id, "SAVED", "status-before-email")
        refreshed = self._preview(content)
        self.assertNotEqual(refreshed["preview_id"], preview["preview_id"])
        self.assertEqual(refreshed["expected_tracking_sequence"], 1)
        self.assertEqual(len(self._audit("EMAIL_STATUS_PREVIEW")), 2)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.url)
        self.store.migrate()
        self.app = FastAPI()
        self.app.include_router(create_email_status_router(self.store))
        self.client = TestClient(self.app)
        history = self.client.get(self.base)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["items"], [])
        # Previews survive restart for future confirmation but are not reported
        # history until the user explicitly applies one.
        self.assertEqual(len(self._audit("EMAIL_STATUS_PREVIEW")), 2)
        self.assertNotIn(private_subject, history.text)
        self.assertNotIn(private_address, history.text)
        self.assertNotIn(private_body, history.text)

    def test_apply_appends_attributed_tracking_event_idempotently_without_changing_application_state(self):
        before = self.store.application(self.application_id)
        preview = self._preview(
            "Subject: Interview\n\nWe would like to invite you to an interview.")
        self.assertEqual(preview["category"], "INTERVIEW_INVITATION")
        self.assertEqual(preview["suggested_status"], "INTERVIEW")
        preview_event = self._audit("EMAIL_STATUS_PREVIEW")[0]
        preview_timestamp = "2026-10-01T00:00:00+00:00"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(
                m.audit.c.id == preview_event["id"]).values(timestamp=preview_timestamp))
        apply_path = self.base + f"/{preview['preview_id']}/apply"
        request = {
            "request_id": "review-interview-1",
            "status": "INTERVIEW",
            "expected_tracking_sequence": preview["expected_tracking_sequence"],
            "reviewed": True,
        }
        applied = self.client.post(apply_path, json=request)
        self.assertEqual(applied.status_code, 200, applied.text)
        result = applied.json()
        self.assertEqual(result["preview_id"], preview["preview_id"])
        self.assertTrue(result["applied"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["status"], "INTERVIEW")
        self.assertEqual(result["attribution"], "USER_REPORTED_EMAIL")
        self.assertEqual(result["sequence"], 1)

        replay = self.client.post(apply_path, json=request)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["idempotent"])
        self.assertEqual(replay.json()["sequence"], result["sequence"])
        changed = self.client.post(apply_path, json={**request, "status": "APPLIED"})
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["detail"]["code"], "EMAIL_STATUS_REQUEST_CONFLICT")
        different_content = self._preview("We are pleased to offer you the position.")
        changed_content_replay = self.client.post(
            self.base + f"/{different_content['preview_id']}/apply",
            json={"request_id": request["request_id"], "status": "OFFER",
                  "expected_tracking_sequence": different_content["expected_tracking_sequence"],
                  "reviewed": True},
        )
        self.assertEqual(changed_content_replay.status_code, 409, changed_content_replay.text)
        self.assertEqual(changed_content_replay.json()["detail"]["code"],
                         "EMAIL_STATUS_REQUEST_CONFLICT")

        history = self.client.get(self.base)
        self.assertEqual(history.status_code, 200, history.text)
        items = history.json()["items"]
        receipt = next(item for item in items if item["preview_id"] == preview["preview_id"])
        self.assertEqual(receipt["decision"], "APPLIED")
        self.assertEqual(receipt["reported_status"], "INTERVIEW")
        self.assertEqual(receipt["attribution"], "USER_REPORTED_EMAIL")
        persisted_receipt = self._audit("EMAIL_STATUS_REVIEW_RECEIPT")[0]
        self.assertEqual(receipt["created_at"], persisted_receipt["timestamp"])
        self.assertNotEqual(receipt["created_at"], preview_timestamp)

        tracking_events = [row for row in self._audit("APPLICATION_PIPELINE_STATUS")
                           if row["entity_id"] != self.application_id]
        email_events = [row for row in tracking_events
                        if row["payload"].get("attribution") == "USER_REPORTED_EMAIL"]
        self.assertEqual(len(email_events), 1)
        self.assertEqual(email_events[0]["payload"]["status"], "INTERVIEW")
        self.assertEqual(email_events[0]["payload"]["sequence"], 1)
        tracking = read_application_tracking(self.store, "fixture", self.application_id)
        self.assertEqual(tracking["status"], "INTERVIEW")
        self.assertEqual(tracking["attribution"], "USER_REPORTED_EMAIL")
        after = self.store.application(self.application_id)
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["confirmation"], before["confirmation"])
        activity = [e for e in list_audit_history(self.store, 'fixture')['events']
                    if e['id'] == email_events[0]['id']]
        self.assertEqual(len(activity), 1)
        self.assertEqual(email_events[0]['payload']['subject_snapshot'],
                         {'subject': activity[0]['subject'], 'company': activity[0]['company']})
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).values(title='Later email title'))
            connection.execute(update(m.companies).values(name='Later email employer'))
        self.assertEqual([e for e in list_audit_history(self.store, 'fixture')['events']
                          if e['id'] == email_events[0]['id']], activity)
        self.assertFalse(any(e['id'] == email_events[0]['id']
                             for e in list_audit_history(self.store, 'other')['events']))

    def test_email_tracking_event_uses_a_separate_request_id_namespace(self):
        email_request_id = "idempotency-collision-fixture"
        set_application_pipeline_status(
            self.store, "fixture", self.application_id, "SAVED",
            "email-status:" + email_request_id,
        )
        preview = self._preview("We would like to invite you to an interview.")
        applied = self.client.post(
            self.base + f"/{preview['preview_id']}/apply",
            json={"request_id": email_request_id, "status": "INTERVIEW",
                  "expected_tracking_sequence": preview["expected_tracking_sequence"],
                  "reviewed": True},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["sequence"], 2)
        tracking = read_application_tracking(self.store, "fixture", self.application_id)
        self.assertEqual(tracking["status"], "INTERVIEW")
        self.assertEqual([row["sequence"] for row in tracking["history"]], [1, 2])

    def test_stale_preview_and_non_applicable_signal_reject_without_mutation(self):
        stale = self._preview("Your application has been received.")
        set_application_pipeline_status(
            self.store, "fixture", self.application_id, "SAVED", "status-after-preview")
        before_status_events = len(self._audit("APPLICATION_PIPELINE_STATUS"))
        stale_response = self.client.post(
            self.base + f"/{stale['preview_id']}/apply",
            json={"request_id": "stale-email-review", "status": "APPLIED",
                  "expected_tracking_sequence": stale["expected_tracking_sequence"],
                  "reviewed": True},
        )
        self.assertEqual(stale_response.status_code, 409, stale_response.text)
        self.assertEqual(stale_response.json()["detail"]["code"], "EMAIL_STATUS_STALE_PREVIEW")
        self.assertEqual(len(self._audit("APPLICATION_PIPELINE_STATUS")), before_status_events)
        self.assertEqual(self._audit("EMAIL_STATUS_REVIEW_RECEIPT"), [])

        ambiguous = self._preview("We may reach out about a possible role in the future.")
        self.assertIn(ambiguous["category"], {"AMBIGUOUS", "UNKNOWN"})
        self.assertIsNone(ambiguous["suggested_status"])
        not_applicable = self.client.post(
            self.base + f"/{ambiguous['preview_id']}/apply",
            json={"request_id": "ambiguous-email-review", "status": "APPLIED",
                  "expected_tracking_sequence": ambiguous["expected_tracking_sequence"],
                  "reviewed": True},
        )
        self.assertEqual(not_applicable.status_code, 409, not_applicable.text)
        self.assertEqual(not_applicable.json()["detail"]["code"], "EMAIL_STATUS_NOT_APPLICABLE")
        self.assertEqual(len(self._audit("APPLICATION_PIPELINE_STATUS")), before_status_events)
        self.assertEqual(self._audit("EMAIL_STATUS_REVIEW_RECEIPT"), [])

    def test_profile_and_application_scope_is_enforced_for_preview_apply_and_history(self):
        foreign_base = f"/profiles/other/applications/{self.application_id}/email-status"
        self.assertEqual(self.client.get(foreign_base).status_code, 404)
        self.assertEqual(self.client.post(foreign_base + "/preview", json={
            "content": "Your application has been received.", "format": "text",
        }).status_code, 404)
        self.assertEqual(self.client.get(
            f"/profiles/fixture/applications/{self.foreign_application_id}/email-status"
        ).status_code, 404)
        self.assertEqual(self.client.get(
            "/profiles/missing/applications/no-such-app/email-status").status_code, 404)

    def test_invalid_request_and_missing_preview_do_not_create_review_events(self):
        invalid_format = self.client.post(self.base + "/preview", json={
            "content": "PRIVATE_INVALID_CONTENT_SENTINEL", "format": "html",
        })
        self.assertEqual(invalid_format.status_code, 422, invalid_format.text)
        self.assertNotIn("PRIVATE_INVALID_CONTENT_SENTINEL", invalid_format.text)
        missing = self.client.post(self.base + "/missing-preview/apply", json={
            "request_id": "unknown-preview", "status": "APPLIED",
            "expected_tracking_sequence": 0, "reviewed": True,
        })
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(self._audit("EMAIL_STATUS_PREVIEW"), [])
        self.assertEqual(self._audit("EMAIL_STATUS_REVIEW_RECEIPT"), [])
        self.assertEqual(self._audit("APPLICATION_PIPELINE_STATUS"), [])


if __name__ == "__main__":
    unittest.main()
