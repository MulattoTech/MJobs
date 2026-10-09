"""Durable, profile-scoped readback for manually reviewed application answers."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ReviewedApplicationAnswerDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-reviewed-answer-durability-")
        self.root = Path(self.temp.name)
        self.database = self.root / "reviewed-answers.sqlite"
        self.database_url = f"sqlite:///{self.database.as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        other = deepcopy(PROFILE)
        other.update(id="other-profile", name="Other Fixture", email="other@example.test")
        self.store.save_profile(other)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _reviewed_profile_payload(self, expected_version, *, name="Alex Fixture"):
        payload = deepcopy(PROFILE)
        payload["name"] = name
        payload["expected_version"] = expected_version
        payload["answers"] = [
            *deepcopy(PROFILE["answers"]),
            {
                "id": "azure-subscriptions-reviewed",
                "question": "Describe your experience managing Azure subscriptions.",
                "text": "I managed Azure subscriptions in the synthetic test environment.",
                "source": "User reviewed application answer",
                "verified": True,
                "user_approved": True,
                "confidence": 100,
            },
        ]
        return payload

    def _save_answer(self, expected_version, *, name="Alex Fixture"):
        return self.client.put(
            "/profiles",
            json=self._reviewed_profile_payload(expected_version, name=name),
        )

    def _answer_bank(self, profile_id="fixture"):
        return self.client.get(
            f"/profiles/{profile_id}/applications/{self.application_id}/answers"
        )

    def _application_snapshot(self):
        with self.store.engine.connect() as connection:
            application = connection.execute(
                select(m.applications).where(m.applications.c.id == self.application_id)
            ).mappings().one()
            return dict(application)

    def test_reviewed_answer_recovers_after_stale_conflict_and_sqlite_reopen(self):
        initial_version = self.store.profile("fixture")["version"]
        application_before = self._application_snapshot()
        attempts_before = self.store.count("application_attempts")
        documents_before = self.store.count("documents")

        first = self._save_answer(initial_version)
        self.assertEqual(first.status_code, 200, first.text)
        saved_version = first.json()["version"]
        first_bank = self._answer_bank()
        self.assertEqual(first_bank.status_code, 200, first_bank.text)
        target = [row for row in first_bank.json()["answers"]
                  if row.get("id") == "azure-subscriptions-reviewed"]
        self.assertEqual(len(target), 1)
        self.assertTrue(target[0]["verified"])
        self.assertTrue(target[0]["user_approved"])

        # An identical optimistic retry resolves to the same profile state and
        # does not append a duplicate answer row.
        retry = self._save_answer(saved_version)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["version"], saved_version)
        retry_bank = self._answer_bank()
        self.assertEqual(
            len([row for row in retry_bank.json()["answers"]
                 if row.get("id") == "azure-subscriptions-reviewed"]),
            1,
        )

        # A competing profile update makes the prior version stale. The failed
        # save leaves the reviewed answer untouched; retrying with the current
        # version recovers without duplicating it.
        competing = self.client.put("/profiles", json={
            **deepcopy(PROFILE),
            "name": "Alex Fixture Updated",
            "answers": self.store.profile("fixture")["answers"],
            "expected_version": saved_version,
        })
        self.assertEqual(competing.status_code, 200, competing.text)
        current_version = competing.json()["version"]
        stale = self._save_answer(saved_version)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.store.profile("fixture")["version"], current_version)
        stale_bank = self._answer_bank()
        self.assertEqual(
            len([row for row in stale_bank.json()["answers"]
                 if row.get("id") == "azure-subscriptions-reviewed"]),
            1,
        )

        recovered = self._save_answer(current_version, name="Alex Fixture Updated")
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["version"], current_version)
        self.assertEqual(self._application_snapshot(), application_before)
        self.assertEqual(self.store.count("application_attempts"), attempts_before)
        self.assertEqual(self.store.count("documents"), documents_before)

        other = self.client.get("/profiles/other-profile")
        self.assertEqual(other.status_code, 200, other.text)
        self.assertNotIn(
            "azure-subscriptions-reviewed",
            {row.get("id") for row in other.json()["answers"]},
        )
        self.assertEqual(
            self.client.get(
                f"/profiles/other-profile/applications/{self.application_id}/answers"
            ).status_code,
            404,
        )

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.store.migrate()
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root))

        reopened = self._answer_bank()
        self.assertEqual(reopened.status_code, 200, reopened.text)
        durable = [row for row in reopened.json()["answers"]
                   if row.get("id") == "azure-subscriptions-reviewed"]
        self.assertEqual(len(durable), 1)
        self.assertEqual(durable[0]["text"],
                         "I managed Azure subscriptions in the synthetic test environment.")
        self.assertTrue(durable[0]["verified"])
        self.assertTrue(durable[0]["user_approved"])
        reopened_other = self.client.get("/profiles/other-profile")
        self.assertEqual(reopened_other.status_code, 200, reopened_other.text)
        self.assertNotIn(
            "azure-subscriptions-reviewed",
            {row.get("id") for row in reopened_other.json()["answers"]},
        )
        self.assertEqual(self._application_snapshot(), application_before)
        self.assertEqual(self.store.count("application_attempts"), attempts_before)
        self.assertEqual(self.store.count("documents"), documents_before)
        self.assertEqual(
            self.client.get(
                f"/profiles/other-profile/applications/{self.application_id}/answers"
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
