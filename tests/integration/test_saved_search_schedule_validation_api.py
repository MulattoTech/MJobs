"""Public API validation and rollback behavior for saved-search cadence."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class SavedSearchScheduleValidationApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-search-schedule-api-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'searches.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def search_body(self, identifier, *, scheduler, name=None, titles=None):
        return {
            "id": identifier,
            "profile_id": "fixture",
            "name": name or f"Search {identifier}",
            "enabled": True,
            "include_titles": titles or ["Systems Engineer"],
            "source_ids": [],
            "scheduler": scheduler,
        }

    def test_malformed_scheduler_objects_are_rejected_with_422(self):
        invalid_schedulers = {
            "missing_enabled": {"interval_seconds": 600},
            "missing_interval": {"enabled": True},
            "extra_key": {"enabled": True, "interval_seconds": 600, "timezone": "UTC"},
            "enabled_integer": {"enabled": 1, "interval_seconds": 600},
            "enabled_string": {"enabled": "true", "interval_seconds": 600},
            "interval_boolean": {"enabled": True, "interval_seconds": True},
            "interval_float": {"enabled": True, "interval_seconds": 600.0},
            "interval_below_minimum": {"enabled": True, "interval_seconds": 299},
            "interval_above_maximum": {"enabled": True, "interval_seconds": 604801},
            "explicit_null": None,
        }

        for case, scheduler in invalid_schedulers.items():
            with self.subTest(case=case):
                response = self.client.put("/search-profiles", json=self.search_body(
                    f"invalid-{case}", scheduler=scheduler))
                self.assertEqual(response.status_code, 422, response.text)

        with self.store.engine.connect() as connection:
            persisted = list(connection.execute(select(m.search_profiles.c.id).where(
                m.search_profiles.c.id.like("invalid-%"))).scalars())
        self.assertEqual(persisted, [])

    def test_rejected_scheduler_update_preserves_existing_search_criteria(self):
        identifier = "existing-cadence"
        valid = self.search_body(identifier, scheduler={
            "enabled": True, "interval_seconds": 900,
        }, titles=["Systems Engineer", "Network Engineer"])
        created = self.client.put("/search-profiles", json=valid)
        self.assertEqual(created.status_code, 200, created.text)
        before = self.client.get(f"/search-profiles/{identifier}")
        self.assertEqual(before.status_code, 200, before.text)
        original = before.json()

        rejected = self.client.put("/search-profiles", json=self.search_body(
            identifier,
            scheduler={"enabled": False},
            name="Changed name must not persist",
            titles=["Changed title must not persist"],
        ))
        self.assertEqual(rejected.status_code, 422, rejected.text)

        after = self.client.get(f"/search-profiles/{identifier}")
        self.assertEqual(after.status_code, 200, after.text)
        persisted = after.json()
        self.assertEqual(persisted["criteria"], original["criteria"])
        self.assertEqual(persisted["criteria"]["scheduler"], {
            "enabled": True, "interval_seconds": 900,
        })
        self.assertEqual(persisted["name"], original["name"])


if __name__ == "__main__":
    unittest.main()
