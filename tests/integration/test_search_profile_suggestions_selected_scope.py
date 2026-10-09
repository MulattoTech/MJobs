"""Read-only selected-saved-search scope for profile title suggestions."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from apps.api.main import create_app
from apps.api.search_profile_suggestions import create_search_profile_suggestions_router
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from tests.integration.test_spine import PROFILE


class SearchProfileSuggestionsSelectedScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-suggestion-selected-scope-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.profile_version = self.store.profile("fixture")["version"]
        self.other_version = self.store.profile("other")["version"]
        self._seed_searches_and_matches()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        self.client.app.include_router(create_search_profile_suggestions_router(self.store))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_searches_and_matches(self):
        timestamp = "2026-10-08T00:00:00+00:00"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "suggestion-selected-company", "name": "Synthetic Employer", "domain": "",
            })
            for search_id, profile_id, titles in (
                ("empty-search", "fixture", []),
                ("selected-search", "fixture", ["System Administrator", "Infrastructure Support Engineer"]),
                ("foreign-search", "other", ["System Administrator"]),
            ):
                connection.execute(insert(m.search_profiles).values(
                    id=search_id, profile_id=profile_id, name=search_id, enabled=False,
                    criteria={"include_titles": titles}, source_ids=[],
                    created_at=timestamp, updated_at=timestamp,
                ))

            matches = (
                ("selected-current", "System Administrator", "fixture", self.profile_version, 95.0),
                ("selected-included", "Infrastructure Support Engineer", "fixture", self.profile_version, 91.0),
                ("selected-suggestion", "IT Operations Engineer", "fixture", self.profile_version, 88.0),
                ("foreign-only", "Platform Administrator", "other", self.other_version, 96.0),
            )
            for job_id, title, profile_id, version, score in matches:
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "suggestion-selected-company", "requisition": job_id,
                    "title": title, "description": "Synthetic selected-search fixture.",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE",
                })
                connection.execute(insert(m.matches).values(
                    id=identity(job_id, profile_id, version), job_id=job_id,
                    profile_id=profile_id, profile_version=version, score=score,
                    missing=[], components={},
                ))

    def test_selected_search_uses_its_own_includes_and_does_not_mutate_storage(self):
        with self.store.engine.connect() as connection:
            searches_before = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
            matches_before = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()]
        workers_before = self.store.count("worker_operations")

        response = self.client.get(
            "/profiles/fixture/search-profile-suggestions",
            params={"current_title": "System Administrator", "search_profile_id": "selected-search"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["profile_version"], self.profile_version)
        self.assertEqual(body["current_title"], "System Administrator")
        self.assertEqual(body["search_profile_id"], "selected-search")
        self.assertEqual(body["suggestions"], [{
            "title": "IT Operations Engineer",
            "strong_match_count": 1,
            "reason": "Observed in 1 current strong match.",
        }])

        with self.store.engine.connect() as connection:
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()], searches_before)
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()], matches_before)
        self.assertEqual(self.store.count("worker_operations"), workers_before)

    def test_missing_foreign_and_stale_searches_share_not_found_without_mutation(self):
        with self.store.engine.connect() as connection:
            searches_before = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
            matches_before = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()]
        workers_before = self.store.count("worker_operations")

        for search_profile_id in ("missing-search", "foreign-search", "stale-search"):
            with self.subTest(search_profile_id=search_profile_id):
                response = self.client.get(
                    "/profiles/fixture/search-profile-suggestions",
                    params={"search_profile_id": search_profile_id},
                )
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["detail"], {
                    "code": "SEARCH_PROFILE_SUGGESTIONS_SEARCH_NOT_FOUND",
                })

        with self.store.engine.connect() as connection:
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()], searches_before)
            self.assertEqual([dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()], matches_before)
        self.assertEqual(self.store.count("worker_operations"), workers_before)

    def test_unselected_suggestions_remain_profile_scoped(self):
        response = self.client.get("/profiles/fixture/search-profile-suggestions")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["search_profile_id"])
        self.assertEqual([item["title"] for item in response.json()["suggestions"]], [
            "Infrastructure Support Engineer", "IT Operations Engineer", "System Administrator",
        ])


if __name__ == "__main__":
    unittest.main()
