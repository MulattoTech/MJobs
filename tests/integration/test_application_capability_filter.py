"""Connected API acceptance for exact-route application capability filtering."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event
from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


class ApplicationCapabilityFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-application-capability-filter-")
        self.root = Path(self.directory.name)
        self.database = self.root / "jobs.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self._seed_routes(self.store, "fixture")
        self.client = self._client(self.store, "FIXTURE")

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _client(self, store, mode):
        return TestClient(create_app(
            store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode=mode))

    def _seed_routes(self, store, prefix):
        routes = (
            # Sorting puts a non-match first so the FIXTURE_ONLY predicate must run before LIMIT.
            ("unsupported", "A Unsupported greenhouse", "greenhouse", "board-a"),
            ("fixture", "B Fixture mock", "mock-ats", "fixture-board"),
            ("review", "C Review required", "unrecognized-provider", "weak-board"),
            ("unknown", "D Unknown route", "unrecognized-provider", "unknown-board"),
            ("exact-mismatch", "E Exact route mismatch", "greenhouse", "route-board"),
        )
        with store.engine.begin() as connection:
            for suffix, title, provider, tenant in routes:
                company_id = f"{prefix}-company-{suffix}"
                job_id = f"{prefix}-job-{suffix}"
                upsert(connection, m.companies, {
                    "id": company_id, "name": title, "domain": "",
                })
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": company_id, "requisition": suffix,
                    "title": title, "description": "Synthetic acceptance fixture.",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE",
                })
                upsert(connection, m.routes, {
                    "id": f"{job_id}-route", "job_id": job_id, "company_id": company_id,
                    "provider": provider, "tenant": tenant, "requisition": suffix,
                    "url": f"https://jobs.example.test/apply/{suffix}",
                })
            # Weak exact route evidence stays review-required. A strong tenant for another
            # route must not rewrite route-board into the acme application adapter.
            for suffix, tenant, confidence in (
                    ("review", "weak-board", 0.45),
                    ("exact-mismatch", "different-board", 0.97)):
                company_id = f"{prefix}-company-{suffix}"
                upsert(connection, m.company_ats_tenants, {
                    "id": f"{prefix}-ats-{suffix}", "company_id": company_id,
                    "provider": "greenhouse", "tenant_identifier": tenant,
                    "organization_identifier": "", "board_identifier": "",
                    "confidence": confidence,
                    "evidence": ["SYNTHETIC_STORED_EVIDENCE"],
                    "first_seen_at": "2026-10-08T00:00:00Z",
                    "last_seen_at": "2026-10-08T00:00:00Z",
                })
            # Deliberately no route for this job: it must never be mislabeled as supported.
            company_id = f"{prefix}-company-no-route"
            upsert(connection, m.companies, {"id": company_id, "name": "F No route", "domain": ""})
            upsert(connection, m.jobs, {
                "id": f"{prefix}-job-no-route", "company_id": company_id,
                "requisition": "no-route", "title": "F No route",
                "description": "Synthetic acceptance fixture.", "skills": [],
                "location": "Remote", "remote_type": "REMOTE",
            })

    def _save_search(self, capability, *, profile_id="fixture"):
        return self.client.put("/search-profiles", json={
            "id": "application-capability-search", "profile_id": profile_id,
            "name": "Fixture application routes", "enabled": False,
            "include_titles": [], "remote": "ANY", "employment_types": [],
            "source_ids": [], "job_filters": {
                "q": "", "remote_type": "", "provider": "", "sort": "title",
                "application_capability": capability,
            },
        })

    def test_exact_route_statuses_fail_closed_and_filter_before_limit(self):
        before_workers = self.store.count("worker_operations")
        before_apps = self.store.count("applications")
        before_attempts = self.store.count("application_attempts")

        with patch("httpx.Client.get", side_effect=AssertionError("Capability reads must not use network")), \
                patch("services.ats_detection.detector.detect_ats",
                      side_effect=AssertionError("Capability reads must not rediscover ATS")):
            fixture_only = self.client.get("/jobs", params={
                "application_capability": "FIXTURE_ONLY", "sort": "title", "limit": 1,
            })
            self.assertEqual(fixture_only.status_code, 200, fixture_only.text)
            self.assertEqual([row["id"] for row in fixture_only.json()["jobs"]],
                             ["fixture-job-fixture"])

            unsupported = self.client.get("/jobs", params={
                "application_capability": "UNSUPPORTED", "sort": "title",
            })
            self.assertEqual(unsupported.status_code, 200, unsupported.text)
            self.assertEqual({row["id"] for row in unsupported.json()["jobs"]},
                             {"fixture-job-unsupported", "fixture-job-exact-mismatch"})

            review = self.client.get("/jobs", params={
                "application_capability": "REVIEW_REQUIRED", "sort": "title",
            })
            self.assertEqual(review.status_code, 200, review.text)
            self.assertEqual([row["id"] for row in review.json()["jobs"]],
                             ["fixture-job-review"])

            unknown = self.client.get("/jobs", params={
                "application_capability": "UNKNOWN", "sort": "title",
            })
            self.assertEqual(unknown.status_code, 200, unknown.text)
            self.assertEqual([row["id"] for row in unknown.json()["jobs"]],
                             ["fixture-job-unknown"])

        all_jobs = self.client.get("/jobs", params={"sort": "title"})
        self.assertEqual(all_jobs.status_code, 200, all_jobs.text)
        self.assertNotIn("fixture-job-no-route", {row["id"] for row in all_jobs.json()["jobs"]})
        self.assertEqual(self.store.count("worker_operations"), before_workers)
        self.assertEqual(self.store.count("applications"), before_apps)
        self.assertEqual(self.store.count("application_attempts"), before_attempts)

    def test_limited_capability_filter_closes_its_database_cursor(self):
        results = []

        def capture(connection, statement, multiparams, params, execution_options, result):
            if "application_route_id" in str(statement):
                results.append(result)

        event.listen(self.store.engine, "after_execute", capture)
        try:
            response = self.client.get("/jobs", params={
                "application_capability": "FIXTURE_ONLY", "sort": "title", "limit": 1,
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()["jobs"]), 1)
            self.assertTrue(results, "The capability-filtered query must be observed")
            self.assertTrue(all(result.closed for result in results),
                            "Returning a limited result must release every database cursor")
        finally:
            event.remove(self.store.engine, "after_execute", capture)
            for result in results:
                result.close()

    def test_saved_filter_round_trip_clear_and_profile_isolation(self):
        saved = self._save_search("FIXTURE_ONLY")
        self.assertEqual(saved.status_code, 200, saved.text)
        listing = self.client.get("/search-profiles", params={"profile_id": "fixture"})
        self.assertEqual(listing.status_code, 200, listing.text)
        entry = next(row for row in listing.json()["search_profiles"]
                     if row["id"] == "application-capability-search")
        self.assertEqual(entry["criteria"]["job_filters"]["application_capability"], "FIXTURE_ONLY")
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.client = self._client(self.store, "FIXTURE")
        restored = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "application-capability-search",
            "sort": "title",
        })
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual([row["id"] for row in restored.json()["jobs"]], ["fixture-job-fixture"])
        self.assertEqual(self.client.get("/jobs", params={
            "profile_id": "other", "search_profile_id": "application-capability-search",
        }).status_code, 409)

        # Explicit empty override clears the selected saved filter without rewriting it.
        cleared_override = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "application-capability-search",
            "application_capability": "", "sort": "title",
        })
        self.assertEqual(cleared_override.status_code, 200, cleared_override.text)
        self.assertIn("fixture-job-unsupported", {row["id"] for row in cleared_override.json()["jobs"]})
        persisted = next(row for row in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"]
                         if row["id"] == "application-capability-search")
        self.assertEqual(persisted["criteria"]["job_filters"]["application_capability"], "FIXTURE_ONLY")

        cleared_saved = self._save_search("")
        self.assertEqual(cleared_saved.status_code, 200, cleared_saved.text)
        cleared_entry = next(row for row in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"]
                             if row["id"] == "application-capability-search")
        self.assertEqual(cleared_entry["criteria"]["job_filters"]["application_capability"], "")

    def test_fixture_only_is_mode_dependent_and_invalid_filter_fails_closed(self):
        invalid = self.client.get("/jobs", params={"application_capability": "SUPPORTED"})
        self.assertEqual(invalid.status_code, 422, invalid.text)

        personal_directory = tempfile.TemporaryDirectory(prefix="mjobs-application-capability-personal-")
        try:
            personal_root = Path(personal_directory.name)
            personal_store = Store(f"sqlite:///{(personal_root / 'jobs.db').as_posix()}")
            personal_store.migrate()
            personal_store.bind_workspace_mode("PERSONAL")
            personal_store.save_profile(dict(PROFILE))
            self._seed_routes(personal_store, "personal")
            with TestClient(create_app(
                    personal_store, "http://127.0.0.1:1", personal_root,
                    "redis://127.0.0.1:1/0", operation_mode="PERSONAL")) as client:
                fixture_only = client.get("/jobs", params={
                    "application_capability": "FIXTURE_ONLY"})
                self.assertEqual(fixture_only.status_code, 200, fixture_only.text)
                self.assertEqual(fixture_only.json()["jobs"], [])
                unsupported = client.get("/jobs", params={
                    "application_capability": "UNSUPPORTED"})
                self.assertIn("personal-job-fixture", {row["id"] for row in unsupported.json()["jobs"]})
            personal_store.engine.dispose()
        finally:
            personal_directory.cleanup()


if __name__ == "__main__":
    unittest.main()
