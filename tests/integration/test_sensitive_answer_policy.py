"""Connected acceptance for profile-scoped sensitive answer preferences."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class SensitiveAnswerPolicyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-sensitive-policy-")
        self.root = Path(self.temp.name)
        self.database = self.root / "sensitive.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
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

    def _save_profile(self, profile):
        current = self.client.get(f"/profiles/{profile['id']}")
        self.assertEqual(current.status_code, 200, current.text)
        payload = {
            key: profile[key]
            for key in ("id", "name", "email", "contact", "preferences")
            if key in profile
        }
        payload["facts"] = [
            {key: row[key] for key in
             ("id", "kind", "text", "source", "verified", "user_approved", "confidence")}
            for row in profile.get("facts", [])
        ]
        payload["answers"] = [
            {key: row[key] for key in
             ("id", "question", "text", "source", "verified", "user_approved", "confidence")
             if key in row}
            for row in profile.get("answers", [])
        ]
        payload["expected_version"] = current.json()["version"]
        return self.client.put("/profiles", json=payload)

    def test_omitted_sensitive_policy_defaults_to_review_and_policy_is_profile_scoped_durable(self):
        initial = self.client.get("/profiles/fixture").json()
        policies = initial.get("preferences", {}).get("sensitive_answer_policies", {})
        self.assertEqual(policies, {})
        self.assertEqual(policies.get("RACE_ETHNICITY", {}).get("mode", "ALWAYS_REQUIRE_REVIEW"),
                         "ALWAYS_REQUIRE_REVIEW")

        initial["preferences"]["sensitive_answer_policies"] = {
            "RACE_ETHNICITY": {"mode": "PREFER_NOT_TO_ANSWER"},
            "SEX_GENDER": {
                "mode": "USE_CONFIGURED_RESPONSE",
                "response": "SYNTHETIC_USER_CONFIGURED_RESPONSE_6f96",
            },
        }
        saved = self._save_profile(initial)
        self.assertEqual(saved.status_code, 200, saved.text)
        other = self.client.get("/profiles/other-profile").json()
        self.assertEqual(other.get("preferences", {}).get("sensitive_answer_policies", {}), {})

        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.client.close()
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root))
        reloaded = self.client.get("/profiles/fixture").json()
        self.assertEqual(reloaded["preferences"]["sensitive_answer_policies"], {
            "RACE_ETHNICITY": {"mode": "PREFER_NOT_TO_ANSWER", "response": None},
            "SEX_GENDER": {
                "mode": "USE_CONFIGURED_RESPONSE",
                "response": "SYNTHETIC_USER_CONFIGURED_RESPONSE_6f96",
            },
        })

    def test_unrecognized_or_invalid_policy_never_falls_back_to_a_configured_answer(self):
        profile = self.client.get("/profiles/fixture").json()
        invalid = deepcopy(profile)
        invalid["preferences"]["sensitive_answer_policies"] = {
            "SEX_GENDER": {"mode": "USE_CONFIGURED_RESPONSE"},
        }
        response = self._save_profile(invalid)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("USE_CONFIGURED_RESPONSE requires an explicit response",
                      str(response.json()))
        self.assertNotIn("SYNTHETIC_USER_CONFIGURED_RESPONSE", str(response.json()))

        unknown_category = deepcopy(profile)
        unknown_category["preferences"]["sensitive_answer_policies"] = {
            "UNRECOGNIZED_CATEGORY": {"mode": "USE_CONFIGURED_RESPONSE", "response": "synthetic"},
        }
        response = self._save_profile(unknown_category)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("UNRECOGNIZED_CATEGORY", str(response.json()))

        unchanged = self.client.get("/profiles/fixture").json()
        self.assertEqual(unchanged.get("preferences", {}).get("sensitive_answer_policies", {}), {})

    def test_configured_demographic_text_stays_out_of_answer_bank_and_requires_review_for_export(self):
        secret = "SYNTHETIC_USER_CONFIGURED_RESPONSE_6f96"
        profile = self.client.get("/profiles/fixture").json()
        profile["preferences"]["sensitive_answer_policies"] = {
            "SEX_GENDER": {"mode": "USE_CONFIGURED_RESPONSE", "response": secret},
        }
        response = self._save_profile(profile)
        self.assertEqual(response.status_code, 200, response.text)

        bank = self.client.get(
            f"/profiles/fixture/applications/{self.application_id}/answers")
        self.assertEqual(bank.status_code, 200, bank.text)
        self.assertNotIn(secret, str(bank.json()))

        export_url = (
            f"/profiles/fixture/applications/{self.application_id}/answers/export")
        item = {"question": "What is your gender identity?", "text": secret, "reviewed": False}
        before_documents = self.store.count("documents")
        before_attempts = self.store.count("application_attempts")
        denied = self.client.post(export_url, json={"profile_id": "fixture", "items": [item]})
        self.assertEqual(denied.status_code, 422, denied.text)
        self.assertEqual(self.store.count("documents"), before_documents)

        reviewed = self.client.post(export_url, json={
            "profile_id": "fixture",
            "items": [{**item, "reviewed": True}],
        })
        self.assertEqual(reviewed.status_code, 201, reviewed.text)
        self.assertEqual(reviewed.json()["external_submission"], "OFF")
        self.assertEqual(self.store.count("application_attempts"), before_attempts)
        document = self.client.get(
            f"/profiles/fixture/documents/{reviewed.json()['document_id']}/txt")
        self.assertEqual(document.status_code, 200, document.text)
        self.assertIn(secret, document.text)
        bank_after_export = self.client.get(
            f"/profiles/fixture/applications/{self.application_id}/answers")
        self.assertNotIn(secret, str(bank_after_export.json()))


if __name__ == "__main__":
    unittest.main()
