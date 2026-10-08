"""API contract for per-saved-search cadence updates."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SavedSearchScheduleApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_scheduler_only_update_persists_cadence_and_preserves_criteria(self):
        created = self.client.put("/search-profiles", json={
            "id": "remote-infra", "profile_id": "fixture", "name": "Remote infrastructure",
            "include_titles": ["System Administrator"], "remote": "REQUIRED",
            "employment_types": ["FULL_TIME"], "source_ids": [],
        })
        self.assertEqual(created.status_code, 200, created.text)

        updated = self.client.put("/search-profiles", json={
            "id": "remote-infra", "profile_id": "fixture", "name": "Remote infrastructure",
            "scheduler": {"enabled": True, "interval_seconds": 1800},
        })
        self.assertEqual(updated.status_code, 200, updated.text)

        saved = self.client.get("/search-profiles", params={"profile_id": "fixture"})
        self.assertEqual(saved.status_code, 200, saved.text)
        profile = saved.json()["search_profiles"][0]
        self.assertEqual(profile["criteria"]["include_titles"], ["System Administrator"])
        self.assertEqual(profile["criteria"]["remote"], "REQUIRED")
        self.assertEqual(profile["criteria"]["employment_types"], ["FULL_TIME"])
        self.assertEqual(profile["criteria"]["scheduler"], {"enabled": True, "interval_seconds": 1800})

    def test_invalid_scheduler_interval_is_rejected(self):
        response = self.client.put("/search-profiles", json={
            "id": "invalid", "profile_id": "fixture", "name": "Invalid cadence",
            "scheduler": {"enabled": True, "interval_seconds": 30},
        })
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
