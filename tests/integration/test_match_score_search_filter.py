"""Connected API acceptance for saved minimum match-score filtering."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.discovery.scheduler import schedule_due_sources
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class MatchScoreSearchFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-match-score-filter-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        configure_source(self.store, "match-score-filter-source")
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.version = self.store.profile("fixture")["version"]
        jobs = {row["title"]: row for row in self.store.jobs()}
        self.python_id = jobs["Python Developer"]["id"]
        self.data_id = jobs["Data Engineer"]["id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.version,
                m.matches.c.job_id == self.python_id).values(score=60.0))
            connection.execute(update(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.version,
                m.matches.c.job_id == self.data_id).values(score=75.0))
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.python_id).values(title="A Stale score"))
            connection.execute(update(m.jobs).where(m.jobs.c.id == self.data_id).values(title="B Current score"))
            # A stale high score must not satisfy a current-version filter.
            connection.execute(insert(m.matches).values(
                id="stale-match-score", job_id=self.python_id, profile_id="fixture",
                profile_version="old-version", score=100.0, missing=[], components={}))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _saved(self, profile_id="fixture", job_filters=None):
        return {"id": "score-filter", "profile_id": profile_id, "name": "High match roles",
                "enabled": False, "include_titles": [], "remote": "ANY",
                "employment_types": [], "source_ids": [],
                "job_filters": job_filters or {"q": "", "title": "", "company": "",
                    "location": "", "remote_type": "", "provider": "", "sort": "score",
                    "min_match_score": "75"}}

    def test_minimum_score_is_inclusive_current_version_and_applied_before_limit(self):
        operations_before = self.store.count("worker_operations")
        with self.store.engine.connect() as connection:
            rows_before = connection.execute(select(m.matches)).mappings().all()
        at = self.client.get("/jobs", params={"profile_id": "fixture", "min_match_score": 75,
                                               "sort": "title", "limit": 1})
        self.assertEqual(at.status_code, 200, at.text)
        self.assertEqual([job["title"] for job in at.json()["jobs"]], ["B Current score"])
        above = self.client.get("/jobs", params={"profile_id": "fixture", "min_match_score": 76})
        self.assertEqual([job["title"] for job in above.json()["jobs"]], [])
        zero = self.client.get("/jobs", params={"profile_id": "fixture", "min_match_score": 0})
        self.assertEqual({job["title"] for job in zero.json()["jobs"]},
                         {"A Stale score", "B Current score"})
        lower_endpoint = self.client.get("/jobs", params={"profile_id": "fixture", "min_match_score": 60,
                                                            "title": "A Stale"})
        self.assertEqual([job["title"] for job in lower_endpoint.json()["jobs"]], ["A Stale score"])
        maximum = self.client.get("/jobs", params={"profile_id": "fixture", "min_match_score": 100})
        self.assertEqual(maximum.json()["jobs"], [])
        below = self.client.get("/jobs", params={"profile_id": "fixture", "min_match_score": 61,
                                                   "title": "A Stale"})
        self.assertEqual(below.json()["jobs"], [])
        null_score = self.client.get("/jobs", params={"profile_id": "other", "min_match_score": 0})
        self.assertEqual(null_score.json()["jobs"], [])
        exact = self.client.get("/jobs", params={"profile_id": "fixture", "min_match_score": 75,
                                                   "title": "B Current"})
        self.assertEqual([job["title"] for job in exact.json()["jobs"]], ["B Current score"])
        self.assertEqual(self.client.get("/jobs", params={"min_match_score": -1}).status_code, 422)
        self.assertEqual(self.client.get("/jobs", params={"min_match_score": 101}).status_code, 422)
        self.assertEqual(self.client.get("/jobs", params={"min_match_score": "bad"}).status_code, 422)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.matches)).mappings().all(), rows_before)
        self.assertEqual(self.store.count("worker_operations"), operations_before)
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])

    def test_saved_filter_round_trip_profile_scope_legacy_and_validation(self):
        operations_before = self.store.count("worker_operations")
        payload = self._saved()
        self.assertEqual(self.client.put("/search-profiles", json=payload).status_code, 200)
        listed = self.client.get("/search-profiles", params={"profile_id": "fixture"})
        row = next(item for item in listed.json()["search_profiles"] if item["id"] == "score-filter")
        self.assertEqual(row["criteria"]["job_filters"]["min_match_score"], "75")
        filtered = self.client.get("/jobs", params={"profile_id": "fixture",
                                  "search_profile_id": "score-filter", "sort": "title", "limit": 1})
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual([job["title"] for job in filtered.json()["jobs"]], ["B Current score"])
        lowered = self.client.get("/jobs", params={"profile_id": "fixture",
            "search_profile_id": "score-filter", "min_match_score": 50,
            "override_saved_match_score": True})
        self.assertEqual({job["title"] for job in lowered.json()["jobs"]},
                         {"A Stale score", "B Current score"})
        cleared = self.client.get("/jobs", params={"profile_id": "fixture",
            "search_profile_id": "score-filter", "override_saved_match_score": True})
        self.assertEqual({job["title"] for job in cleared.json()["jobs"]},
                         {"A Stale score", "B Current score"})
        self.assertEqual(self.client.get("/search-profiles", params={"profile_id": "other"})
                         .json()["search_profiles"], [])
        self.assertEqual(self.client.get("/jobs", params={"profile_id": "other",
                         "search_profile_id": "score-filter"}).status_code, 409)
        for malformed in ("-1", "101", "1.5", "bad"):
            with self.subTest(min_match_score=malformed):
                invalid = self.client.put("/search-profiles", json=self._saved(job_filters={
                    "q": "", "remote_type": "", "provider": "", "sort": "score",
                    "min_match_score": malformed}))
                self.assertEqual(invalid.status_code, 422)
        legacy = self.client.put("/search-profiles", json=self._saved(
            job_filters={"q": "", "remote_type": "", "provider": "", "sort": "score"}))
        self.assertEqual(legacy.status_code, 200, legacy.text)
        legacy_row = next(item for item in self.client.get("/search-profiles", params={
            "profile_id": "fixture"}).json()["search_profiles"] if item["id"] == "score-filter")
        self.assertEqual(legacy_row["criteria"]["job_filters"]["salary_min"], "")
        self.assertNotIn("min_match_score", legacy_row["criteria"]["job_filters"])
        self.assertEqual(self.store.count("worker_operations"), operations_before)
        self.assertEqual(schedule_due_sources(self.store)["scheduled"], [])


if __name__ == "__main__":
    unittest.main()
