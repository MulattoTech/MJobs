"""Connected API acceptance for profile-scoped saved Jobs filters."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
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
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer")
                               .values(location="Boston, MA"))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer")
                               .values(location="Berlin"))
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
            "job_filters": job_filters or {"q": "network", "title": "Developer", "location": "Boston",
                                           "company": "Example", "remote_type": "REMOTE",
                                           "provider": "greenhouse", "sort": "company",
                                           "salary_min": "", "salary_max": ""},
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
            "q": "network", "title": "Developer", "location": "Boston", "company": "Example",
            "remote_type": "REMOTE", "provider": "greenhouse", "sort": "company",
            "salary_min": "", "salary_max": ""})
        self.assertFalse(row["enabled"])
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        before = self.store.count("worker_operations")
        tick = schedule_due_sources(self.store)
        self.assertEqual(tick["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), before)

    def test_old_saved_job_filters_without_location_remain_valid(self):
        response = self.client.put("/search-profiles", json=self._profile(
            identifier="old-saved-filter",
            job_filters={"q": "network", "title": "Developer", "company": "Example",
                         "remote_type": "REMOTE", "provider": "greenhouse", "sort": "company"}))
        self.assertEqual(response.status_code, 200, response.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"]
                   if item["id"] == "old-saved-filter")
        self.assertEqual(row["criteria"]["job_filters"]["location"], "")
        self.assertEqual(row["criteria"]["job_filters"]["salary_min"], "")
        self.assertEqual(row["criteria"]["job_filters"]["salary_max"], "")

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

    def test_location_text_is_literal_case_insensitive_and_filters_before_limit(self):
        response = self.client.get("/jobs", params={
            "location": "bOsToN", "sort": "title", "limit": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([job["title"] for job in response.json()["jobs"]], ["Python Developer"])

        for literal in ("%", "_", "Seattle", "REMOTE"):
            with self.subTest(location=literal):
                unmatched = self.client.get("/jobs", params={"location": literal})
                self.assertEqual(unmatched.status_code, 200, unmatched.text)
                self.assertEqual(unmatched.json()["jobs"], [])

        combined = self.client.get("/jobs", params={
            "q": "Python", "title": "Developer", "company": "Example",
            "location": "Boston", "remote_type": "REMOTE", "provider": "mock-ats",
            "sort": "title", "limit": 1})
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual([job["title"] for job in combined.json()["jobs"]], ["Python Developer"])

    def test_annual_usd_salary_filters_are_inclusive_fail_closed_and_apply_before_limit(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Python Developer").values(
                title="A Unknown salary", attributes={"salary_min": 120000, "salary_max": 140000,
                                                       "salary_currency": "EUR", "salary_period": "YEAR"}))
            connection.execute(update(m.jobs).where(m.jobs.c.title == "Data Engineer").values(
                title="B Annual salary", attributes={"salary_min": 100000, "salary_max": 120000,
                                                       "salary_currency": "USD", "salary_period": "YEAR"}))

        operations_before = self.store.count("worker_operations")
        included = self.client.get("/jobs", params={"salary_min": 100000, "salary_max": 120000,
                                                     "sort": "title", "limit": 1})
        self.assertEqual(included.status_code, 200, included.text)
        self.assertEqual([job["title"] for job in included.json()["jobs"]], ["B Annual salary"])

        lower_endpoint = self.client.get("/jobs", params={"salary_min": 120000})
        upper_endpoint = self.client.get("/jobs", params={"salary_max": 100000})
        no_match = self.client.get("/jobs", params={"salary_min": 120001})
        self.assertEqual([job["title"] for job in lower_endpoint.json()["jobs"]], ["B Annual salary"])
        self.assertEqual([job["title"] for job in upper_endpoint.json()["jobs"]], ["B Annual salary"])
        self.assertEqual(no_match.json()["jobs"], [])
        self.assertEqual(self.client.get("/jobs", params={"salary_min": 120001, "salary_max": 100000}).status_code, 422)
        matching_job = next(job for job in self.store.jobs() if job["title"] == "B Annual salary")
        composed = self.client.get("/jobs", params={"q": "B Annual", "title": "B Annual",
                                                     "company": "Example", "location": "Berlin",
                                                     "remote_type": matching_job["remote_type"],
                                                     "provider": "mock-ats", "salary_min": 100000,
                                                     "salary_max": 120000, "limit": 1})
        self.assertEqual(composed.status_code, 200, composed.text)
        self.assertEqual([job["title"] for job in composed.json()["jobs"]], ["B Annual salary"])
        for metadata in ({}, {"salary_min": 120000, "salary_max": 140000,
                              "salary_currency": "USD", "salary_period": "HOUR"},
                         {"salary_min": "competitive", "salary_max": "negotiable",
                          "salary_currency": "USD", "salary_period": "YEAR"}):
            with self.subTest(metadata=metadata):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.jobs).where(m.jobs.c.title == "A Unknown salary")
                                       .values(attributes=metadata))
                filtered = self.client.get("/jobs", params={"salary_min": 100000})
                self.assertEqual([job["title"] for job in filtered.json()["jobs"]], ["B Annual salary"])
        self.assertEqual(self.store.count("worker_operations"), operations_before)

    def test_salary_bounds_round_trip_as_profile_scoped_filters_without_side_effects(self):
        filters = {"q": "network", "title": "Developer", "location": "Boston", "company": "Example",
                   "remote_type": "REMOTE", "provider": "greenhouse", "sort": "company",
                   "salary_min": "85000", "salary_max": "130000"}
        operations_before = self.store.count("worker_operations")
        response = self.client.put("/search-profiles", json=self._profile(job_filters=filters))
        self.assertEqual(response.status_code, 200, response.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "saved-filter")
        self.assertEqual(row["criteria"]["job_filters"], filters)
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        malformed = self.client.put("/search-profiles", json=self._profile(
            identifier="inverted-salary", job_filters={**filters, "salary_min": "130001"}))
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(self.store.count("worker_operations"), operations_before)
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)


if __name__ == "__main__":
    unittest.main()
