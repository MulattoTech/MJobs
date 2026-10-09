"""The job source facet is distinct from application and ATS providers."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.discovery.sources.collect import configure_source
from services.discovery.scheduler import schedule_due_sources
from tests.integration.test_spine import PROFILE


class JobSourceFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-job-source-filter-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(root / 'jobs.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.invalid"})
        self.source_a = configure_source(self.store, "source-a")
        self.source_b = configure_source(self.store, "source-b")
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        with self.store.engine.begin() as connection:
            python_job = connection.execute(select(m.jobs.c.id).where(
                m.jobs.c.title == "Python Developer")).scalar_one()
            data_job = connection.execute(select(m.jobs.c.id).where(
                m.jobs.c.title == "Data Engineer")).scalar_one()
            connection.execute(update(m.jobs).where(m.jobs.c.id == python_job).values(
                attributes={"source_id": self.source_a}))
            connection.execute(update(m.jobs).where(m.jobs.c.id == data_job).values(
                attributes={"source_id": self.source_b}))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        self.addCleanup(self.client.close)

    def _search_profile(self, source_id: str, *, identifier="saved-source-filter"):
        return {
            "id": identifier, "profile_id": "fixture", "name": f"Source A jobs {identifier}",
            "enabled": False, "include_titles": [], "remote": "ANY", "employment_types": [],
            "source_ids": [],
            "job_filters": {
                "q": "", "remote_type": "", "provider": "", "sort": "title",
                "job_source_id": source_id,
            },
        }

    def test_source_filter_composes_before_limit_and_is_not_an_ats_filter(self):
        unfiltered = self.client.get("/jobs", params={"sort": "title", "limit": 1})
        self.assertEqual(unfiltered.status_code, 200, unfiltered.text)
        self.assertEqual([row["title"] for row in unfiltered.json()["jobs"]], ["Data Engineer"])

        source_a = self.client.get("/jobs", params={
            "job_source_id": self.source_a, "sort": "title", "limit": 1,
        })
        self.assertEqual(source_a.status_code, 200, source_a.text)
        self.assertEqual([row["title"] for row in source_a.json()["jobs"]], ["Python Developer"])
        self.assertEqual(source_a.json()["jobs"][0]["provider"], "mock-ats")

        source_b = self.client.get("/jobs", params={"job_source_id": self.source_b, "sort": "title"})
        self.assertEqual([row["title"] for row in source_b.json()["jobs"]], ["Data Engineer"])
        self.assertEqual(self.client.get("/jobs", params={
            "job_source_id": "not-a-configured-source",
        }).status_code, 422)

    def test_saved_source_filter_round_trips_clears_and_stays_profile_scoped(self):
        saved = self.client.put("/search-profiles", json=self._search_profile(self.source_a))
        self.assertEqual(saved.status_code, 200, saved.text)
        saved_rows = self.client.get("/search-profiles", params={
            "profile_id": "fixture",
        }).json()["search_profiles"]
        restored_source = next(row for row in saved_rows if row["id"] == "saved-source-filter")
        self.assertEqual(restored_source["criteria"]["job_filters"]["job_source_id"], self.source_a)
        selected = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "saved-source-filter",
            "job_source_id": restored_source["criteria"]["job_filters"]["job_source_id"],
            "sort": "title",
        })
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([row["title"] for row in selected.json()["jobs"]], ["Python Developer"])
        self.assertEqual(self.client.get("/jobs", params={
            "profile_id": "other", "search_profile_id": "saved-source-filter",
        }).status_code, 409)

        saved_again = self.client.put("/search-profiles", json=self._search_profile(""))
        self.assertEqual(saved_again.status_code, 200, saved_again.text)
        cleared = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "saved-source-filter", "sort": "title",
        })
        self.assertEqual([row["title"] for row in cleared.json()["jobs"]],
                         ["Data Engineer", "Python Developer"])

        legacy = self._search_profile(self.source_a, identifier="legacy-source-filter")
        del legacy["job_filters"]["job_source_id"]
        response = self.client.put("/search-profiles", json=legacy)
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.client.get("/search-profiles", params={
            "profile_id": "fixture",
        }).json()["search_profiles"]
        self.assertEqual(next(row for row in stored if row["id"] == "legacy-source-filter")
                         ["criteria"]["job_filters"]["job_source_id"], "")

        before = self.store.count("worker_operations")
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), before)
