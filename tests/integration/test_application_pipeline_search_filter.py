"""Connected API acceptance for user-reported application pipeline status filters."""
# Expected first red on the unmodified base: /jobs does not yet accept or apply
# application_pipeline_status, while search-profile validation rejects that key.
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.documents.generate import prepare
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class ApplicationPipelineSearchFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-pipeline-status-filter-")
        self.root = Path(self.directory.name)
        self.database = self.root / "jobs.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.jobs = {row["title"]: row for row in self.store.jobs("fixture")}
        self.assertIn("Python Developer", self.jobs)
        self.assertIn("Data Engineer", self.jobs)
        self.applications = {}
        for profile_id in ("fixture", "other"):
            for title in ("Python Developer", "Data Engineer"):
                self.applications[profile_id, title] = prepare(
                    self.store, self.jobs[title]["id"], profile_id, self.root)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _report(self, profile_id, title, status, request_id):
        application_id = self.applications[profile_id, title]
        response = self.client.put(
            f"/profiles/{profile_id}/applications/{application_id}/tracking/status",
            json={"status": status, "request_id": request_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _save_search(self, *, status, profile_id="fixture", identifier="status-search"):
        filters = {"q": "", "remote_type": "", "provider": "", "sort": "title",
                   "application_pipeline_status": status}
        return self.client.put("/search-profiles", json={
            "id": identifier, "profile_id": profile_id, "name": "Reported application status",
            "enabled": False, "include_titles": [], "remote": "ANY", "employment_types": [],
            "source_ids": [], "job_filters": filters})

    def test_status_filter_uses_latest_reported_event_and_fails_closed_for_invalid_values(self):
        self._report("fixture", "Python Developer", "SAVED", "python-saved")
        self._report("fixture", "Python Developer", "APPLIED", "python-applied")
        self._report("fixture", "Data Engineer", "INTERVIEW", "data-interview")

        latest = self.client.get("/jobs", params={
            "profile_id": "fixture", "application_pipeline_status": "APPLIED"})
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual([job["title"] for job in latest.json()["jobs"]], ["Python Developer"])
        stale = self.client.get("/jobs", params={
            "profile_id": "fixture", "application_pipeline_status": "SAVED"})
        self.assertEqual(stale.status_code, 200, stale.text)
        self.assertEqual(stale.json()["jobs"], [])
        invalid = self.client.get("/jobs", params={
            "profile_id": "fixture", "application_pipeline_status": "SUBMISSION_CONFIRMED"})
        self.assertEqual(invalid.status_code, 422, invalid.text)

    def test_not_tracked_includes_prepared_application_with_no_user_reported_event(self):
        # Both fixture applications exist, but no status event has been reported by the user.
        response = self.client.get("/jobs", params={
            "profile_id": "fixture", "application_pipeline_status": "NOT_TRACKED"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({job["title"] for job in response.json()["jobs"]},
                         {"Python Developer", "Data Engineer"})

    def test_status_filter_is_profile_scoped_and_applied_before_limit(self):
        # A later status for profile B on the same job must not be visible to profile A.
        self._report("fixture", "Python Developer", "APPLIED", "a-applied")
        self._report("other", "Python Developer", "OFFER", "b-offer")
        self._report("fixture", "Data Engineer", "INTERVIEW", "a-interview")

        foreign_only = self.client.get("/jobs", params={
            "profile_id": "fixture", "application_pipeline_status": "OFFER"})
        self.assertEqual(foreign_only.status_code, 200, foreign_only.text)
        self.assertEqual(foreign_only.json()["jobs"], [])
        own = self.client.get("/jobs", params={
            "profile_id": "other", "application_pipeline_status": "OFFER"})
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual([job["title"] for job in own.json()["jobs"]], ["Python Developer"])

        # Data Engineer sorts first but does not match; filtering must happen before LIMIT.
        limited = self.client.get("/jobs", params={
            "profile_id": "fixture", "application_pipeline_status": "APPLIED",
            "sort": "title", "limit": 1})
        self.assertEqual(limited.status_code, 200, limited.text)
        self.assertEqual([job["title"] for job in limited.json()["jobs"]], ["Python Developer"])

    def test_saved_search_status_round_trip_clear_and_owner_isolation_without_side_effects(self):
        self._report("fixture", "Python Developer", "APPLIED", "saved-applied")
        operations_before = self.store.count("worker_operations")
        applications_before = self.store.count("applications")
        attempts_before = self.store.count("attempts")
        with self.store.engine.connect() as connection:
            jobs_before = connection.execute(select(m.jobs.c.id).order_by(m.jobs.c.id)).all()
            status_events_before = connection.execute(select(m.audit.c.id).where(
                m.audit.c.action == "APPLICATION_PIPELINE_STATUS")).all()

        saved = self._save_search(status="APPLIED")
        self.assertEqual(saved.status_code, 200, saved.text)
        row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "status-search")
        self.assertEqual(row["criteria"]["job_filters"]["application_pipeline_status"], "APPLIED")
        results = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "status-search"})
        self.assertEqual(results.status_code, 200, results.text)
        self.assertEqual([job["title"] for job in results.json()["jobs"]], ["Python Developer"])
        wrong_owner = self.client.get("/jobs", params={
            "profile_id": "other", "search_profile_id": "status-search"})
        self.assertEqual(wrong_owner.status_code, 409)
        self.assertEqual(self.client.get("/search-profiles", params={
            "profile_id": "other"}).json()["search_profiles"], [])

        cleared = self._save_search(status="")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        cleared_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "status-search")
        self.assertEqual(cleared_row["criteria"]["job_filters"]["application_pipeline_status"], "")
        invalid_saved = self._save_search(status="SUBMISSION_CONFIRMED", identifier="invalid-status-search")
        self.assertEqual(invalid_saved.status_code, 422, invalid_saved.text)

        self.assertEqual(self.store.count("worker_operations"), operations_before)
        self.assertEqual(self.store.count("applications"), applications_before)
        self.assertEqual(self.store.count("attempts"), attempts_before)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.jobs.c.id).order_by(m.jobs.c.id)).all(), jobs_before)
            self.assertEqual(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action == "APPLICATION_PIPELINE_STATUS")).all(), status_events_before)
        self.assertTrue(all(self.store.application(application_id)["confirmation"] is None
                            for application_id in self.applications.values()))


if __name__ == "__main__":
    unittest.main()
