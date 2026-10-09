"""Connected profile API acceptance for reviewed, reusable application answers."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


class ReviewedApplicationAnswerSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-reviewed-answer-save-")
        self.root = Path(self.temp.name)
        self.database = self.root / "reviewed-answer.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile({**PROFILE, "answers": []})
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other profile",
                                 "email": "other@example.invalid", "answers": []})
        self.profile_id = "fixture"
        self.application_id = "application-fixture"
        self._seed_application()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="PERSONAL",
        ))
        self.answers_url = (
            f"/profiles/{self.profile_id}/applications/{self.application_id}/answers"
        )

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _seed_application(self):
        profile = self.store.profile(self.profile_id)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "company-fixture", "name": "Fictional employer",
                "domain": "fictional.example",
            })
            upsert(connection, m.jobs, {
                "id": "job-fixture", "company_id": "company-fixture",
                "requisition": "job-fixture", "title": "Systems Administrator",
                "description": "Synthetic integration fixture.", "skills": [],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "route-fixture", "job_id": "job-fixture",
                "company_id": "company-fixture", "provider": "manual",
                "tenant": "fictional", "requisition": "job-fixture",
                "url": "https://fictional.example/jobs/fixture",
            })
            upsert(connection, m.applications, {
                "id": self.application_id, "job_id": "job-fixture",
                "route_id": "route-fixture", "profile_id": self.profile_id,
                "profile_version": profile["version"], "state": "PREPARED",
                "target": "LOCAL", "fact_ids": [], "trace": {},
                "document_version": 1, "confirmation": None,
                "created_at": "2026-10-09T00:00:00+00:00", "submitted_at": None,
            })

    def _profile_payload(self, profile, answers):
        facts = [{
            "id": row.get("fact_id", row["id"]),
            "kind": row.get("kind", "skill"), "text": row["text"],
            "source": row["source"], "confidence": row["confidence"],
            "verified": row["verified"], "user_approved": row["user_approved"],
        } for row in profile.get("facts", [])]
        return {
            "id": profile["id"], "name": profile["name"], "email": profile["email"],
            "expected_version": profile["version"], "contact": profile["contact"],
            "preferences": profile["preferences"], "facts": facts, "answers": answers,
        }

    def _exported_documents(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.documents).where(
                m.documents.c.kind == "application-answer-export",
            )).scalar_one()

    def _application_attempts(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.attempts).where(
                m.attempts.c.application_id == self.application_id,
            )).scalar_one()

    def test_answer_is_saved_only_after_explicit_confirmation_and_reads_back_idempotently(self):
        before_profile = self.client.get(f"/profiles/{self.profile_id}")
        self.assertEqual(before_profile.status_code, 200, before_profile.text)
        self.assertEqual(before_profile.json()["answers"], [])
        before_bank = self.client.get(self.answers_url)
        self.assertEqual(before_bank.status_code, 200, before_bank.text)
        self.assertEqual(before_bank.json()["answers"], [])
        profile_saved_events = self.store.count("audit_events")

        # No profile mutation is issued until the user confirms accuracy and reuse.
        still_unconfirmed = self.client.get(self.answers_url)
        self.assertEqual(still_unconfirmed.json()["answers"], [])
        self.assertEqual(self.store.count("audit_events"), profile_saved_events)

        confirmed_answer = {
            "id": "reviewed-linux-outage-answer",
            "question": "Describe a time you resolved a production outage.",
            "text": "In a synthetic example, I reviewed logs and restored the service.",
            "source": "Explicit user review for Fictional employer",
            "verified": True, "user_approved": True, "confidence": 91,
        }
        payload = self._profile_payload(before_profile.json(), [confirmed_answer])
        saved = self.client.put("/profiles", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)

        readback = self.client.get(self.answers_url)
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["answers"], [confirmed_answer])
        profile_after_save = self.client.get(f"/profiles/{self.profile_id}").json()

        replay_payload = self._profile_payload(profile_after_save, [confirmed_answer])
        replay = self.client.put("/profiles", json=replay_payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["version"], saved.json()["version"])
        replayed_readback = self.client.get(self.answers_url)
        self.assertEqual(replayed_readback.json()["answers"], [confirmed_answer])

        other = self.client.get(
            f"/profiles/other/applications/{self.application_id}/answers"
        )
        self.assertEqual(other.status_code, 404)

    def test_sensitive_answer_bank_reuse_is_rejected_without_export_or_submission(self):
        profile = self.client.get(f"/profiles/{self.profile_id}").json()
        sensitive_answer = {
            "id": "sensitive-work-authorization",
            "question": "Are you authorized to work in the United States?",
            "text": "Synthetic test response; not a real applicant statement.",
            "source": "Explicit user review fixture", "verified": True,
            "user_approved": True, "confidence": 100,
        }
        saved = self.client.put(
            "/profiles", json=self._profile_payload(profile, [sensitive_answer]),
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        current = self.client.get(f"/profiles/{self.profile_id}").json()
        self.assertEqual(current["answers"], [sensitive_answer])

        documents_before = self._exported_documents()
        attempts_before = self._application_attempts()
        application_before = self.store.application(self.application_id)
        response = self.client.post(
            self.answers_url + "/export",
            json={"profile_id": self.profile_id, "items": [{
                "question": sensitive_answer["question"],
                "answer_id": sensitive_answer["id"],
                "text": sensitive_answer["text"], "reviewed": True,
            }]},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Sensitive or legal", response.text)
        self.assertEqual(self._exported_documents(), documents_before)
        self.assertEqual(self._application_attempts(), attempts_before)
        application_after = self.store.application(self.application_id)
        self.assertEqual(application_after["state"], application_before["state"])
        self.assertEqual(application_after["submitted_at"], application_before["submitted_at"])
        self.assertEqual(application_after["confirmation"], application_before["confirmation"])
        self.assertEqual(self.client.get(self.answers_url).json()["answers"], [sensitive_answer])


if __name__ == "__main__":
    unittest.main()
