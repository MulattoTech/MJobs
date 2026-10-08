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


class SearchProfileSuggestionsValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-title-suggestions-validation-")
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
        self.client.app.include_router(create_search_profile_suggestions_router(self.store))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_matches(self):
        rows = (
            ("stale", "Stale Platform Administrator", "fixture", "old-version", 99.0),
            ("weak", "Weak Data Analyst", "fixture", self.current_version, 74.0),
            ("other", "Other Profile Engineer", "other", self.other_version, 99.0),
        )
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "validation-company", "name": "Synthetic Validation", "domain": "",
            })
            for job_id, title, profile_id, version, score in rows:
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "validation-company", "requisition": job_id,
                    "title": title, "description": "Synthetic negative acceptance fixture.",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE",
                })
                connection.execute(insert(m.matches).values(
                    id=identity(job_id, profile_id, version), job_id=job_id,
                    profile_id=profile_id, profile_version=version,
                    score=score, missing=[], components={},
                ))

    def _seed_ranked_current_suggestions(self):
        expected_counts = {
            f"Candidate {index:02d}": 1 + index % 5
            for index in range(22)
        }
        with self.store.engine.begin() as connection:
            for index, (title, count) in enumerate(expected_counts.items()):
                for occurrence in range(count):
                    job_id = f"ranked-{index:02d}-{occurrence}"
                    upsert(connection, m.jobs, {
                        "id": job_id, "company_id": "validation-company", "requisition": job_id,
                        "title": title, "description": "Synthetic bounded suggestion fixture.",
                        "skills": [], "location": "Remote", "remote_type": "REMOTE",
                    })
                    connection.execute(insert(m.matches).values(
                        id=identity(job_id, "fixture", self.current_version), job_id=job_id,
                        profile_id="fixture", profile_version=self.current_version,
                        score=75.0, missing=[], components={},
                    ))
        return expected_counts

    def _save_existing_search_profile(self):
        response = self.client.put("/search-profiles", json={
            "id": "saved-search",
            "profile_id": "fixture",
            "name": "Original system administration criteria",
            "enabled": False,
            "include_titles": ["System Administrator"],
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [],
            "job_filters": {
                "q": "",
                "remote_type": "",
                "provider": "",
                "sort": "score",
            },
        })
        self.assertEqual(response.status_code, 200, response.text)

    def test_weak_stale_and_other_profile_only_matches_produce_no_suggestions(self):
        response = self.client.get("/profiles/fixture/search-profile-suggestions")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["profile_id"], "fixture")
        self.assertEqual(response.json()["profile_version"], self.current_version)
        self.assertEqual(response.json()["suggestions"], [])

    def test_missing_profile_and_malformed_current_title_fail_closed(self):
        missing_profile = self.client.get("/profiles/missing/search-profile-suggestions")
        self.assertEqual(missing_profile.status_code, 404, missing_profile.text)

        malformed_query = self.client.get(
            "/profiles/fixture/search-profile-suggestions",
            params={"current_title": "x" * 501},
        )
        self.assertEqual(malformed_query.status_code, 422, malformed_query.text)

    def test_results_are_bounded_deterministic_and_explain_only_current_counts(self):
        expected_counts = self._seed_ranked_current_suggestions()
        self._save_existing_search_profile()
        with self.store.engine.connect() as connection:
            searches_before = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
        workers_before = self.store.count("worker_operations")
        first = self.client.get("/profiles/fixture/search-profile-suggestions")
        second = self.client.get("/profiles/fixture/search-profile-suggestions")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        rows = first.json()["suggestions"]
        self.assertEqual(rows, second.json()["suggestions"])
        self.assertLessEqual(len(rows), 20)

        expected = sorted(
            expected_counts.items(), key=lambda item: (-item[1], item[0].casefold()),
        )[:20]
        self.assertEqual(
            [(row["title"], row["strong_match_count"]) for row in rows], expected,
        )
        self.assertEqual(
            [row["reason"] for row in rows],
            [
                f"Observed in {count} current strong match{'es' if count != 1 else ''}."
                for _, count in expected
            ],
        )
        self.assertNotIn("Stale Platform Administrator", {row["title"] for row in rows})
        self.assertNotIn("Other Profile Engineer", {row["title"] for row in rows})
        self.assertNotIn("Weak Data Analyst", {row["title"] for row in rows})
        with self.store.engine.connect() as connection:
            searches_after = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
        self.assertEqual(searches_after, searches_before)
        self.assertEqual(self.store.count("worker_operations"), workers_before)

    def test_no_match_read_does_not_create_search_criteria_or_worker_work(self):
        with self.store.engine.connect() as connection:
            searches_before = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
            matches_before = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()]
        workers_before = self.store.count("worker_operations")

        response = self.client.get("/profiles/fixture/search-profile-suggestions")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["suggestions"], [])
        with self.store.engine.connect() as connection:
            searches_after = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
            matches_after = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()]
        self.assertEqual(searches_after, searches_before)
        self.assertEqual(matches_after, matches_before)
        self.assertEqual(self.store.count("worker_operations"), workers_before)


if __name__ == "__main__":
    unittest.main()
