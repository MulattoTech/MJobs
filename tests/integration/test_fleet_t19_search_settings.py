"""Public saved Jobs filter updates preserve scope and scheduler safety."""
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
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class FleetT19SearchSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-t19-search-settings-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'settings.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        configure_source(self.store, "t19-search-settings")
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.client = self._client(self.store)
        self.addCleanup(self.client.close)

    def _client(self, store):
        return TestClient(create_app(
            store, "http://127.0.0.1:1", self.root,
            operation_mode="FIXTURE",
        ))

    @staticmethod
    def _filters(title):
        return {
            "q": "", "title": title, "company": "", "location": "",
            "remote_type": "", "provider": "", "sort": "title",
        }

    def _create_disabled_saved_jobs_filter(self):
        response = self.client.put("/search-profiles", json={
            "id": "t19-saved-jobs",
            "profile_id": "fixture",
            "name": "Fixture saved Jobs filter",
            "enabled": False,
            "job_filters": self._filters("Python"),
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def _profile_row(self, client):
        response = client.get("/search-profiles/t19-saved-jobs")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _audit_snapshot(self, store):
        with store.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(m.audit).order_by(m.audit.c.id),
            ).mappings()]

    def test_partial_filter_edit_stays_disabled_and_filters_after_reopen(self):
        self._create_disabled_saved_jobs_filter()

        edited = self.client.put("/search-profiles", json={
            "id": "t19-saved-jobs",
            "profile_id": "fixture",
            "name": "Fixture saved Jobs filter",
            "job_filters": self._filters("Data"),
        })

        self.assertEqual(edited.status_code, 200, edited.text)
        reopened = Store(self.store.url)
        try:
            with self._client(reopened) as client:
                profile = self._profile_row(client)
                self.assertFalse(profile["enabled"])
                stored_filters = profile["criteria"]["job_filters"]
                requested_filters = self._filters("Data")
                self.assertEqual(
                    {key: stored_filters[key] for key in requested_filters},
                    requested_filters,
                )
                jobs = client.get("/jobs", params={
                    "profile_id": "fixture",
                    "search_profile_id": "t19-saved-jobs",
                    "title": stored_filters["title"],
                    "sort": "title",
                })
                self.assertEqual(jobs.status_code, 200, jobs.text)
                self.assertEqual([row["title"] for row in jobs.json()["jobs"]],
                                 ["Data Engineer"])
        finally:
            reopened.engine.dispose()

    def test_partial_enable_with_existing_ui_filters_is_rejected_without_writes(self):
        self._create_disabled_saved_jobs_filter()
        before_profile = self._profile_row(self.client)
        before_audit = self._audit_snapshot(self.store)

        attempted = self.client.put("/search-profiles", json={
            "id": "t19-saved-jobs",
            "profile_id": "fixture",
            "name": "Changed name must not be saved",
            "enabled": True,
        })

        self.assertEqual(attempted.status_code, 422, attempted.text)
        self.assertEqual(self._profile_row(self.client), before_profile)
        self.assertEqual(self._audit_snapshot(self.store), before_audit)
        reopened = Store(self.store.url)
        try:
            with self._client(reopened) as client:
                self.assertEqual(self._profile_row(client), before_profile)
                self.assertEqual(self._audit_snapshot(reopened), before_audit)
        finally:
            reopened.engine.dispose()

    def test_foreign_profile_update_is_rejected_without_writes(self):
        self._create_disabled_saved_jobs_filter()
        before_profile = self._profile_row(self.client)
        before_audit = self._audit_snapshot(self.store)

        attempted = self.client.put("/search-profiles", json={
            "id": "t19-saved-jobs",
            "profile_id": "other",
            "name": "Foreign profile must not take ownership",
        })

        self.assertEqual(attempted.status_code, 409, attempted.text)
        self.assertEqual(self._profile_row(self.client), before_profile)
        self.assertEqual(self._audit_snapshot(self.store), before_audit)
        reopened = Store(self.store.url)
        try:
            with self._client(reopened) as client:
                self.assertEqual(self._profile_row(client), before_profile)
                self.assertEqual(self._audit_snapshot(reopened), before_audit)
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
