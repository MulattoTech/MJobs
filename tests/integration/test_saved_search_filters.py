"""Connected API acceptance for profile-scoped saved Jobs filters."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.scheduler import schedule_due_sources
from services.discovery.pipeline import discover
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class SavedSearchFiltersApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-saved-search-filters-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.source_id = configure_source(self.store, "saved-filter-source")
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _profile(self, *, identifier="saved-filter", profile_id="fixture", job_filters=None):
        return {
            "id": identifier, "profile_id": profile_id, "name": "Remote admin roles",
            "enabled": False, "include_titles": ["System Administrator"],
            "remote": "REQUIRED", "employment_types": [], "source_ids": [],
            "job_filters": job_filters or {"q": "network", "title": "Developer",
                                           "company": "Example", "remote_type": "REMOTE",
                                           "provider": "greenhouse", "sort": "newest"},
        }

    def test_saved_jobs_filters_persist_profile_scope_and_do_not_schedule_discovery(self):
        response = self.client.put("/search-profiles", json=self._profile())
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.client.get("/search-profiles", params={"profile_id": "fixture"})
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in saved.json()["search_profiles"]
                   if item["id"] == "saved-filter")

        self.assertEqual(row["criteria"]["include_titles"], ["System Administrator"])
        self.assertEqual(row["criteria"]["remote"], "REQUIRED")
        self.assertEqual(row["criteria"]["job_filters"], {
            "q": "network", "title": "Developer", "company": "Example",
            "remote_type": "REMOTE", "provider": "greenhouse", "sort": "newest"})
        self.assertFalse(row["enabled"])
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])

        before = self.store.count("worker_operations")
        tick = schedule_due_sources(self.store)
        self.assertEqual(tick["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), before)

    def test_saved_filter_validation_and_cross_profile_update_rejection(self):
        self.client.put("/search-profiles", json=self._profile()).raise_for_status()
        malformed = self.client.put("/search-profiles", json=self._profile(
            identifier="malformed", job_filters={"q": "x", "remote_type": "LUNAR"}))
        self.assertEqual(malformed.status_code, 422)

        cross_profile = self.client.put("/search-profiles", json=self._profile(
            profile_id="other", job_filters={"q": "x", "title": "", "company": "",
                                               "remote_type": "REMOTE", "provider": "", "sort": "score"}))
        self.assertNotEqual(cross_profile.status_code, 200, cross_profile.text)
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])

        wrong_owner = self.client.get("/jobs", params={
            "profile_id": "other", "search_profile_id": "saved-filter"})
        self.assertEqual(wrong_owner.status_code, 409)

    def test_title_and_company_are_literal_case_insensitive_and_filter_before_limit(self):
        response = self.client.get("/jobs", params={
            "title": "pYtHoN", "company": "eXaMpLe", "sort": "title", "limit": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([job["title"] for job in response.json()["jobs"]], ["Python Developer"])

        title_literal = self.client.get("/jobs", params={"title": "%", "limit": 1})
        company_literal = self.client.get("/jobs", params={"company": "_", "limit": 1})
        self.assertEqual(title_literal.status_code, 200, title_literal.text)
        self.assertEqual(company_literal.status_code, 200, company_literal.text)
        self.assertEqual(title_literal.json()["jobs"], [])
        self.assertEqual(company_literal.json()["jobs"], [])

        combined = self.client.get("/jobs", params={
            "q": "Python", "title": "Developer", "company": "Example", "limit": 10})
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual([job["title"] for job in combined.json()["jobs"]], ["Python Developer"])


if __name__ == "__main__":
    unittest.main()
