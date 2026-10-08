"""Connected API acceptance for read-only search-profile title suggestions."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from tests.integration.test_spine import PROFILE


class SearchProfileSuggestionsApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-search-profile-suggestions-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.current_version = self.store.profile("fixture")["version"]
        self.other_version = self.store.profile("other")["version"]
        self._seed_matches()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        # The integration owner mounts this router in main.py. Mount locally here
        # so the route contract is independently testable on its assigned branch.
        from apps.api.search_profile_suggestions import create_search_profile_suggestions_router
        self.client.app.include_router(create_search_profile_suggestions_router(self.store))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_matches(self):
        jobs = (
            ("strong-infrastructure", "Infrastructure Support Engineer", "fixture", self.current_version, 91.0),
            ("strong-operations-a", "IT Operations Engineer", "fixture", self.current_version, 86.0),
            ("strong-operations-b", "IT Operations Engineer", "fixture", self.current_version, 80.0),
            ("current-title", "System Administrator", "fixture", self.current_version, 99.0),
            ("weak-title", "Data Analyst", "fixture", self.current_version, 74.0),
            ("stale-title", "Platform Administrator", "fixture", "stale-profile-version", 99.0),
            ("other-profile-title", "Platform Administrator", "other", self.other_version, 99.0),
        ) + tuple(
            (f"strong-profile-specialist-{index:02d}",
             f"Profile Specialist {index:02d}", "fixture", self.current_version, 75.0)
            for index in range(20)
        )
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "suggestion-company", "name": "Synthetic Systems", "domain": "",
            })
            for job_id, title, profile_id, version, score in jobs:
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "suggestion-company", "requisition": job_id,
                    "title": title, "description": "Synthetic suggestion acceptance fixture.",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE",
                })
                connection.execute(insert(m.matches).values(
                    id=identity(job_id, profile_id, version), job_id=job_id,
                    profile_id=profile_id, profile_version=version,
                    score=score, missing=[], components={},
                ))

    def test_suggestions_use_current_profile_matches_and_are_read_only(self):
        with self.store.engine.connect() as connection:
            matches_before = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()]
            searches_before = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
        workers_before = self.store.count("worker_operations")

        response = self.client.get(
            "/profiles/fixture/search-profile-suggestions",
            params={"current_title": "System Administrator"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["profile_version"], self.current_version)
        self.assertEqual(body["current_title"], "System Administrator")
        suggestions = body["suggestions"]
        self.assertEqual(len(suggestions), 20)
        self.assertEqual(
            [(row["title"], row["strong_match_count"], row["reason"])
             for row in suggestions],
            [("IT Operations Engineer", 2,
              "Observed in 2 current strong matches.")]
            + [(title, 1, "Observed in 1 current strong match.") for title in (
                "Infrastructure Support Engineer",
                *(f"Profile Specialist {index:02d}" for index in range(18)),
            )],
        )

        with self.store.engine.connect() as connection:
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()], matches_before)
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()], searches_before)
        self.assertEqual(self.store.count("worker_operations"), workers_before)

    def test_profile_scope_and_missing_profile_errors(self):
        response = self.client.get("/profiles/other/search-profile-suggestions")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["profile_id"], "other")
        self.assertEqual(response.json()["suggestions"], [{
            "title": "Platform Administrator",
            "strong_match_count": 1,
            "reason": "Observed in 1 current strong match.",
        }])

        missing = self.client.get("/profiles/unknown/search-profile-suggestions")
        self.assertEqual(missing.status_code, 404, missing.text)


if __name__ == "__main__":
    unittest.main()
