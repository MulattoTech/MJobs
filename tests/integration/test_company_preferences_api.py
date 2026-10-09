"""HTTP contract for profile-scoped company preferences."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from packages.persistence import models as m
from packages.persistence.store import Store
from apps.api.company_preferences import create_company_preferences_router
from tests.integration.test_spine import PROFILE


class CompanyPreferencesApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-company-preferences-api-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(root / 'company-preferences.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Northwind Systems", domain="northwind.example"))
        app = FastAPI()
        app.include_router(create_company_preferences_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_read_defaults_to_neutral_and_write_is_profile_scoped_and_idempotent(self):
        url = "/profiles/fixture/company-preferences/company"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "profile_id": "fixture", "company_id": "company",
            "status": "NEUTRAL", "updated_at": None,
        })

        saved = self.client.put(url, json={"status": "BLACKLISTED", "request_id": "blacklist-1"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["status"], "BLACKLISTED")
        self.assertFalse(saved.json()["replayed"])
        self.assertTrue(saved.json()["updated_at"])
        replay = self.client.put(url, json={"status": "BLACKLISTED", "request_id": "blacklist-1"})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.client.get(url).json()["status"], "BLACKLISTED")
        self.assertEqual(self.client.get(url.replace("fixture", "other")).json()["status"], "NEUTRAL")

    def test_invalid_payload_unknown_scope_and_conflicting_replay_fail_closed(self):
        url = "/profiles/fixture/company-preferences/company"
        self.assertEqual(self.client.put(url, json={"status": "BLOCKED", "request_id": "bad"}).status_code, 422)
        self.assertEqual(self.client.put(url, json={"status": "PREFERRED"}).status_code, 422)
        self.assertEqual(self.client.get("/profiles/missing/company-preferences/company").status_code, 404)
        self.assertEqual(self.client.get("/profiles/fixture/company-preferences/missing").status_code, 404)
        self.assertEqual(self.client.put(url, json={"status": "BLACKLISTED", "request_id": "same"}).status_code, 200)
        conflict = self.client.put(url, json={"status": "PREFERRED", "request_id": "same"})
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_preference_list_contains_latest_user_decision_and_company_name(self):
        url = "/profiles/fixture/company-preferences/company"
        self.client.put(url, json={"status": "BLACKLISTED", "request_id": "list-1"})
        self.client.put(url, json={"status": "PREFERRED", "request_id": "list-2"})
        result = self.client.get("/profiles/fixture/company-preferences")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"profile_id": "fixture", "preferences": [{
            "profile_id": "fixture", "company_id": "company", "company": "Northwind Systems",
            "status": "PREFERRED", "updated_at": self.client.get(url).json()["updated_at"],
        }]})
