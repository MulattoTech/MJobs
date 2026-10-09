"""Profile-scoped durable API contract for multipage application progress."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.application_multipage_state import create_application_multipage_state_router
from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationMultipageStateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-application-multipage-state-")
        self.root = Path(self.temp.name)
        self.database = self.root / "multipage.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        self.url = f"/profiles/fixture/applications/{self.application_id}/multipage-state"
        self.client = self._client()

    def _client(self):
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="FIXTURE")
        app.include_router(create_application_multipage_state_router(self.store))
        return TestClient(app)

    def _audit_snapshot(self):
        with self.store.engine.connect() as connection:
            rows = connection.execute(
                select(m.audit).order_by(m.audit.c.id),
            ).mappings().all()
        return [dict(row) for row in rows]

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_progress_navigation_review_and_reload_keep_one_scoped_state(self):
        initial_payload = {
            "request_id": "save-identity-1",
            "action": "SAVE_PROGRESS",
            "expected_version": 0,
            "page_id": "identity",
            "page_order": ["identity", "experience"],
            "progress": {"identity": {"full_name": "Synthetic Applicant"}},
        }
        saved = self.client.post(self.url, json=initial_payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        initial = saved.json()
        self.assertEqual(initial["profile_id"], "fixture")
        self.assertEqual(initial["application_id"], self.application_id)
        self.assertEqual(initial["page_order"], ["identity", "experience"])
        self.assertEqual(initial["current_page_id"], "identity")
        self.assertEqual(initial["status"], "IN_PROGRESS")
        self.assertEqual(initial["progress"], initial_payload["progress"])
        self.assertEqual(initial["version"], 1)
        self.assertIs(initial["manual_intervention_required"], True)
        self.assertNotIn("submit", initial)
        self.assertNotIn("submission", initial)

        replay = self.client.post(self.url, json=initial_payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), initial)

        changed_replay = self.client.post(self.url, json={
            **initial_payload,
            "progress": {"identity": {"full_name": "Changed Applicant"}},
        })
        self.assertEqual(changed_replay.status_code, 409, changed_replay.text)
        self.assertEqual(self.client.get(self.url).json(), initial)

        next_payload = {
            "request_id": "next-experience-1",
            "action": "NEXT",
            "expected_version": 1,
            "page_id": "identity",
            "next_page_id": "experience",
            "progress": {"identity": {"full_name": "Synthetic Applicant"}},
        }
        advanced = self.client.post(self.url, json=next_payload)
        self.assertEqual(advanced.status_code, 200, advanced.text)
        self.assertEqual(advanced.json()["current_page_id"], "experience")
        self.assertEqual(advanced.json()["version"], 2)

        previous_payload = {
            "request_id": "previous-identity-1",
            "action": "PREVIOUS",
            "expected_version": 2,
            "page_id": "experience",
            "previous_page_id": "identity",
            "progress": {"identity": {"full_name": "Synthetic Applicant"}},
        }
        returned = self.client.post(self.url, json=previous_payload)
        self.assertEqual(returned.status_code, 200, returned.text)
        self.assertEqual(returned.json()["current_page_id"], "identity")
        self.assertEqual(returned.json()["version"], 3)

        advanced_again = self.client.post(self.url, json={
            "request_id": "next-experience-2",
            "action": "NEXT",
            "expected_version": 3,
            "page_id": "identity",
            "next_page_id": "experience",
            "progress": {"identity": {"full_name": "Synthetic Applicant"}},
        })
        self.assertEqual(advanced_again.status_code, 200, advanced_again.text)
        self.assertEqual(advanced_again.json()["current_page_id"], "experience")

        review_payload = {
            "request_id": "review-1",
            "action": "REVIEW",
            "expected_version": 4,
            "page_id": "experience",
            "progress": {"identity": {"full_name": "Synthetic Applicant"}},
        }
        reviewed = self.client.post(self.url, json=review_payload)
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["status"], "REVIEW")
        self.assertEqual(reviewed.json()["version"], 5)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.client = self._client()
        readback = self.client.get(self.url)
        self.assertEqual(readback.status_code, 200, readback.text)
        persisted = readback.json()
        self.assertEqual(persisted["current_page_id"], "review")
        self.assertEqual(persisted["status"], "REVIEW")
        self.assertEqual(persisted["progress"], review_payload["progress"])
        self.assertEqual(persisted["version"], 5)
        application = self.store.application(self.application_id)
        self.assertEqual(application["state"], "PREPARED")
        self.assertIsNone(application["confirmation"])
        self.assertNotIn("submission", application)

    def test_foreign_owner_stale_version_and_submit_field_cannot_change_progress(self):
        payload = {
            "request_id": "save-owner-1",
            "action": "SAVE_PROGRESS",
            "expected_version": 0,
            "page_id": "identity",
            "page_order": ["identity", "experience"],
            "progress": {"identity": {"full_name": "Synthetic Applicant"}},
        }
        wrong_owner_url = self.url.replace("/profiles/fixture/", "/profiles/other/")
        foreign = self.client.post(wrong_owner_url, json=payload)
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(self.client.get(wrong_owner_url).status_code, 404)

        created = self.client.post(self.url, json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        profile_before = self.store.profile("fixture")
        application_before = self.store.application(self.application_id)
        audit_before = self._audit_snapshot()
        progress_before = self.client.get(self.url).json()
        stale = self.client.post(self.url, json={
            **payload,
            "request_id": "stale-save-1",
            "expected_version": 0,
            "progress": {"identity": {"full_name": "Stale Applicant"}},
        })
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json(), {"detail": {"code": "MULTIPAGE_VERSION_CONFLICT"}})
        self.assertNotIn("submit", stale.json())
        self.assertNotIn("submission", stale.json())
        self.assertEqual(self.store.profile("fixture"), profile_before)
        self.assertEqual(self.store.application(self.application_id), application_before)
        self.assertEqual(self._audit_snapshot(), audit_before)
        self.assertEqual(self.client.get(self.url).json(), progress_before)
        self.assertEqual(progress_before, created.json())

        attempted_submit = self.client.post(self.url, json={
            **payload,
            "request_id": "attempt-submit-1",
            "expected_version": 1,
            "submit": True,
            "progress": {"identity": {"full_name": "Private Synthetic Applicant"}},
        })
        self.assertEqual(attempted_submit.status_code, 422, attempted_submit.text)
        self.assertNotIn("Private Synthetic Applicant", attempted_submit.text)
        self.assertEqual(self.client.get(self.url).json(), created.json())
        application = self.store.application(self.application_id)
        self.assertEqual(application["state"], "PREPARED")
        self.assertIsNone(application["confirmation"])


if __name__ == "__main__":
    unittest.main()
