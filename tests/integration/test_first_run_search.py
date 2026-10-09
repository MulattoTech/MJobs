"""A first-run collect applies the saved search selected by the user."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class FirstRunSearchApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-first-run-search-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'wizard.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.source_id = configure_source(self.store, "wizard-board", provider="greenhouse")
        self.app = create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def save_search(self, *, search_id="wizard-search", profile_id="fixture", source_ids=None, enabled=True):
        save_search_profile(self.store, {
            "id": search_id,
            "profile_id": profile_id,
            "name": "Systems administrator roles",
            "enabled": enabled,
            "include_titles": ["Systems Administrator"],
            "remote": "REQUIRED",
            "employment_types": ["FULL_TIME"],
            "source_ids": source_ids if source_ids is not None else [self.source_id],
        })

    def test_collect_queues_the_selected_saved_search(self):
        self.save_search()
        with TestClient(self.app) as client:
            response = client.post(f"/sources/{self.source_id}/collect", json={
                "profile_id": "fixture",
                "request_id": "first-run-wizard-search",
                "search_profile_id": "wizard-search",
            })
        self.assertEqual(response.status_code, 202, response.text)
        with self.store.engine.connect() as connection:
            params = connection.execute(
                select(m.operations.c.params).where(m.operations.c.id == response.json()["operation_id"])
            ).scalar_one()
        self.assertEqual(params["source_id"], self.source_id)
        self.assertEqual(params["search_profile_id"], "wizard-search")

    def test_collect_rejects_saved_search_from_another_profile_before_queueing(self):
        other_profile = {**PROFILE, "id": "other", "name": "Other profile", "email": "other@example.test"}
        self.store.save_profile(other_profile)
        self.save_search(search_id="other-search", profile_id="other")
        with TestClient(self.app) as client:
            response = client.post(f"/sources/{self.source_id}/collect", json={
                "profile_id": "fixture",
                "request_id": "first-run-wizard-cross-profile",
                "search_profile_id": "other-search",
            })
        self.assertEqual(response.status_code, 409, response.text)
        with self.store.engine.connect() as connection:
            count = connection.execute(select(m.operations.c.id)).all()
        self.assertEqual(count, [])

    def test_collect_rejects_saved_search_scoped_to_a_different_source(self):
        other_source_id = configure_source(self.store, "other-wizard-board", provider="greenhouse")
        self.save_search(source_ids=[other_source_id])
        with TestClient(self.app) as client:
            response = client.post(f"/sources/{self.source_id}/collect", json={
                "profile_id": "fixture",
                "request_id": "first-run-wizard-cross-source",
                "search_profile_id": "wizard-search",
            })
        self.assertEqual(response.status_code, 409, response.text)
        with self.store.engine.connect() as connection:
            count = connection.execute(select(m.operations.c.id)).all()
        self.assertEqual(count, [])

    def test_collect_rejects_disabled_saved_search_before_queueing(self):
        self.save_search(enabled=False)
        with TestClient(self.app) as client:
            response = client.post(f"/sources/{self.source_id}/collect", json={
                "profile_id": "fixture",
                "request_id": "first-run-wizard-disabled-search",
                "search_profile_id": "wizard-search",
            })
        self.assertEqual(response.status_code, 409, response.text)
        with self.store.engine.connect() as connection:
            count = connection.execute(select(m.operations.c.id)).all()
        self.assertEqual(count, [])
