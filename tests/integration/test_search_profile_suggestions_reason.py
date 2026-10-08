"""API acceptance for evidence-count reasons without saved-search mutation."""
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


class SearchProfileSuggestionsReasonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-suggestion-reason-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.profile_version = self.store.profile("fixture")["version"]
        self._seed_search_and_matches()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))
        # Router mounting is owned by the integration branch; this test isolates
        # its profile-scoped connected API contract on the feature candidate.
        self.client.app.include_router(create_search_profile_suggestions_router(self.store))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _seed_search_and_matches(self):
        timestamp = "2026-10-08T00:00:00+00:00"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "suggestion-reason-company", "name": "Synthetic Employer", "domain": "",
            })
            connection.execute(insert(m.search_profiles).values(
                id="suggestion-reason-search", profile_id="fixture", name="System Administrator",
                enabled=False,
                criteria={"include_titles": ["System Administrator"],
                          "job_filters": {"title": "System Administrator"}},
                source_ids=[], created_at=timestamp, updated_at=timestamp,
            ))
            matches = (
                ("reason-strong-one", "Infrastructure Support Engineer", self.profile_version, 91.0),
                ("reason-strong-two", "Infrastructure Support Engineer", self.profile_version, 75.0),
                ("reason-weak", "Infrastructure Support Engineer", self.profile_version, 74.0),
                ("reason-stale", "Infrastructure Support Engineer", "stale-version", 100.0),
            )
            for job_id, title, version, score in matches:
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "suggestion-reason-company",
                    "requisition": job_id, "title": title,
                    "description": "Synthetic evidence-count fixture.",
                    "skills": [], "location": "Remote", "remote_type": "REMOTE",
                })
                connection.execute(insert(m.matches).values(
                    id=identity(job_id, "fixture", version), job_id=job_id,
                    profile_id="fixture", profile_version=version, score=score,
                    missing=[], components={},
                ))

    def test_reason_reports_current_strong_match_count_without_mutating_search_or_workers(self):
        with self.store.engine.connect() as connection:
            searches_before = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
            matches_before = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()]
        workers_before = self.store.count("worker_operations")

        response = self.client.get(
            "/profiles/fixture/search-profile-suggestions",
            params={"current_title": "System Administrator"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_version"], self.profile_version)
        self.assertEqual(body["current_title"], "System Administrator")
        self.assertEqual(body["suggestions"], [{
            "title": "Infrastructure Support Engineer",
            "strong_match_count": 2,
            "reason": "Observed in 2 current strong matches.",
        }])

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
