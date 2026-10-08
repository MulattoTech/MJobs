"""Connected acceptance for the saved application-link availability facet."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.discovery.pipeline import discover
from services.discovery.scheduler import schedule_due_sources
from tests.integration.test_spine import PROFILE


class ApplicationLinkFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-application-link-filter-")
        self.root = Path(self.directory.name)
        self.database_url = f"sqlite:///{(self.root / 'jobs.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self._seed_link_status_jobs()
        self.client = self._client()

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _client(self):
        return TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def _seed_link_status_jobs(self):
        with self.store.engine.connect() as connection:
            base = connection.execute(select(m.jobs.c.company_id).limit(1)).scalar_one()
        fixtures = (
            ("available", "Z Available Link", {"application_url_status": "PROVIDER_SUPPLIED"}),
            ("unavailable", "Y Unavailable Link", {"application_url_status": "UNAVAILABLE"}),
            ("unknown", "A Unknown Link", {"application_url_status": "INFERRED"}),
            ("missing", "B Missing Link", {}),
        )
        with self.store.engine.begin() as connection:
            for suffix, title, attributes in fixtures:
                job_id = f"application-link-{suffix}"
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": base, "requisition": job_id,
                    "title": title, "description": "Synthetic acceptance fixture.",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE",
                    "attributes": attributes,
                })
                upsert(connection, m.routes, {
                    "id": f"route-{job_id}", "job_id": job_id, "company_id": base,
                    "provider": "fixture", "tenant": "application-link-filter",
                    "requisition": job_id,
                    "url": f"https://jobs.example.test/apply/{job_id}",
                })

    def _filters(self, **changes):
        return ({
            "q": "", "title": "", "company": "", "location": "",
            "salary_min": "", "salary_max": "", "min_match_score": "",
            "experience_level": "", "employment_type": "", "posted_within_days": "",
            "remote_type": "", "provider": "", "skill": "",
            "education_requirement": "", "clearance_level": "", "ats_provider": "",
            "application_link_status": "",
            "sort": "title",
        } | changes)

    def _profile(self, *, profile_id="fixture", name="Link status search", filters=None):
        return {
            "id": "application-link-saved-search", "profile_id": profile_id,
            "name": name, "enabled": False, "include_titles": [], "remote": "ANY",
            "employment_types": [], "source_ids": [],
            "job_filters": filters if filters is not None else self._filters(),
        }

    def test_filter_is_exact_fail_closed_and_composes_before_limit(self):
        before_workers = self.store.count("worker_operations")
        before_apps = self.store.count("applications")

        available = self.client.get("/jobs", params={
            "application_link_status": "AVAILABLE", "sort": "title", "limit": 1,
        })
        self.assertEqual(available.status_code, 200, available.text)
        self.assertEqual([row["id"] for row in available.json()["jobs"]],
                         ["application-link-available"])

        unavailable = self.client.get("/jobs", params={
            "application_link_status": "UNAVAILABLE", "sort": "title", "limit": 1,
        })
        self.assertEqual(unavailable.status_code, 200, unavailable.text)
        self.assertEqual([row["id"] for row in unavailable.json()["jobs"]],
                         ["application-link-unavailable"])

        # The title clause composes with availability as AND, before LIMIT.
        composed = self.client.get("/jobs", params={
            "application_link_status": "AVAILABLE", "title": "Z Available",
            "sort": "title", "limit": 1,
        })
        self.assertEqual([row["id"] for row in composed.json()["jobs"]],
                         ["application-link-available"])
        self.assertEqual(self.client.get("/jobs", params={
            "application_link_status": "AVAILABLE", "title": "Y Unavailable",
        }).json()["jobs"], [])

        # Unknown and absent metadata must not be relabeled as either state.
        for status in ("AVAILABLE", "UNAVAILABLE"):
            with self.subTest(status=status):
                ids = {row["id"] for row in self.client.get("/jobs", params={
                    "application_link_status": status, "sort": "title",
                }).json()["jobs"]}
                self.assertNotIn("application-link-unknown", ids)
                self.assertNotIn("application-link-missing", ids)

        self.assertEqual(self.store.count("worker_operations"), before_workers)
        self.assertEqual(self.store.count("applications"), before_apps)

    def test_saved_filter_survives_reopen_is_profile_scoped_and_clears(self):
        saved = self.client.put("/search-profiles", json=self._profile(
            filters=self._filters(application_link_status="AVAILABLE")))
        self.assertEqual(saved.status_code, 200, saved.text)

        listing = self.client.get("/search-profiles", params={"profile_id": "fixture"})
        self.assertEqual(listing.status_code, 200, listing.text)
        entry = next(row for row in listing.json()["search_profiles"]
                     if row["id"] == "application-link-saved-search")
        self.assertEqual(entry["criteria"]["job_filters"]["application_link_status"], "AVAILABLE")
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        selected = self.client.get("/jobs", params={
            "profile_id": "fixture", "search_profile_id": "application-link-saved-search",
            "sort": "title", "limit": 1,
        })
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([row["id"] for row in selected.json()["jobs"]],
                         ["application-link-available"])
        self.assertEqual(self.client.get("/jobs", params={
            "profile_id": "other", "search_profile_id": "application-link-saved-search",
        }).status_code, 409)

        cleared = self.client.put("/search-profiles", json=self._profile(
            filters=self._filters(application_link_status="")))
        self.assertEqual(cleared.status_code, 200, cleared.text)
        cleared_row = next(row for row in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"]
                           if row["id"] == "application-link-saved-search")
        self.assertEqual(cleared_row["criteria"]["job_filters"]["application_link_status"], "")
        all_jobs = self.client.get("/jobs", params={"sort": "title", "limit": 20})
        self.assertEqual(all_jobs.status_code, 200, all_jobs.text)
        self.assertIn("application-link-unknown", {row["id"] for row in all_jobs.json()["jobs"]})

        operations_before = self.store.count("worker_operations")
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])
        self.assertEqual(self.store.count("worker_operations"), operations_before)
        self.assertEqual(self.store.count("applications"), 0)


if __name__ == "__main__":
    unittest.main()
