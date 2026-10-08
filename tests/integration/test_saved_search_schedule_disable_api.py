"""Scheduler toggles update cadence without rewriting the saved search."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class SavedSearchScheduleDisableApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-search-schedule-disable-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'searches.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})
        self.source_id = configure_source(self.store, "scheduler-toggle-source")
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_disabling_scheduler_preserves_saved_search_scope_and_filters(self):
        identifier = "remote-systems-roles"
        original_body = {
            "id": identifier,
            "profile_id": "fixture",
            "name": "Remote systems roles",
            "enabled": True,
            "include_titles": ["Systems Engineer", "Network Engineer"],
            "required_keywords": ["Active Directory"],
            "remote": "REQUIRED",
            "source_ids": [self.source_id],
            "scheduler": {"enabled": True, "interval_seconds": 900},
        }
        created = self.client.put("/search-profiles", json=original_body)
        self.assertEqual(created.status_code, 200, created.text)
        before_response = self.client.get(f"/search-profiles/{identifier}")
        self.assertEqual(before_response.status_code, 200, before_response.text)
        before = before_response.json()
        with self.store.engine.connect() as connection:
            operations_before = list(connection.execute(select(m.operations.c.id)).scalars())

        disabled = self.client.put("/search-profiles", json={
            "id": identifier,
            "profile_id": "fixture",
            "name": before["name"],
            "scheduler": {"enabled": False, "interval_seconds": 900},
        })
        self.assertEqual(disabled.status_code, 200, disabled.text)

        after_response = self.client.get(f"/search-profiles/{identifier}")
        self.assertEqual(after_response.status_code, 200, after_response.text)
        after = after_response.json()
        self.assertEqual(after["criteria"]["scheduler"], {
            "enabled": False, "interval_seconds": 900,
        })
        self.assertEqual(after["source_ids"], before["source_ids"])
        self.assertEqual(after["name"], before["name"])
        expected_criteria = dict(before["criteria"])
        expected_criteria["scheduler"] = {"enabled": False, "interval_seconds": 900}
        self.assertEqual(after["criteria"], expected_criteria)
        self.assertEqual(after["profile_id"], "fixture")
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        with self.store.engine.connect() as connection:
            operations_after = list(connection.execute(select(m.operations.c.id)).scalars())
        self.assertEqual(operations_after, operations_before)


if __name__ == "__main__":
    unittest.main()
