"""Profile-scoped, read-only API contract for cheap job prefilter previews."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.cheap_prefilter import create_cheap_prefilter_router
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class CheapPrefilterApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-cheap-prefilter-api-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})
        self.fixture_source_id = configure_source(self.store, "fixture-board", provider="greenhouse")
        self.other_source_id = configure_source(self.store, "other-board", provider="greenhouse")
        self.search_profile_id = save_search_profile(self.store, {
            "id": "fixture-search", "profile_id": "fixture", "name": "Fixture search",
            "enabled": False, "include_titles": ["Systems Administrator"],
            "exclude_titles": [], "required_keywords": ["Linux"],
            "optional_keywords": [], "excluded_keywords": [], "exact_phrases": [],
            "technology_aliases": {}, "remote": "ANY", "employment_types": [],
            "source_ids": [self.fixture_source_id],
        })
        self.other_search_profile_id = save_search_profile(self.store, {
            "id": "other-search", "profile_id": "other", "name": "Other search",
            "enabled": False, "include_titles": ["Accountant"],
            "exclude_titles": [], "required_keywords": [], "optional_keywords": [],
            "excluded_keywords": [], "exact_phrases": [], "technology_aliases": {},
            "remote": "ANY", "employment_types": [], "source_ids": [self.other_source_id],
        })
        self.no_source_search_profile_id = save_search_profile(self.store, {
            "id": "no-source-search", "profile_id": "fixture", "name": "No source search",
            "enabled": False, "include_titles": [], "exclude_titles": [],
            "required_keywords": [], "optional_keywords": [], "excluded_keywords": [],
            "exact_phrases": [], "technology_aliases": {}, "remote": "ANY",
            "employment_types": [], "source_ids": [],
        })
        self._seed_posting(self.fixture_source_id, "match", "Systems Administrator", "Linux operations")
        self._seed_posting(self.fixture_source_id, "wrong-title", "Accountant", "Linux operations")
        self._seed_posting(self.fixture_source_id, "wrong-keyword", "Systems Administrator", "Windows support")
        self._seed_posting(self.other_source_id, "other-match", "Accountant", "Finance")
        app = FastAPI()
        app.include_router(create_cheap_prefilter_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_posting(self, source_id, suffix, title, description):
        company_id = f"company-{suffix}"
        job_id = f"job-{suffix}"
        posting_id = f"posting-{suffix}"
        raw = {
            "source_job_id": suffix,
            "title": title,
            "company": f"Synthetic {suffix}",
            "description": description,
            "skills": [],
            "location": "Remote",
        }
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": company_id, "name": raw["company"], "domain": ""})
            upsert(connection, m.jobs, {
                "id": job_id, "company_id": company_id, "requisition": suffix,
                "title": title, "description": description, "skills": [],
                "location": "Remote", "remote_type": "REMOTE",
            })
            upsert(connection, m.postings, {
                "id": posting_id, "job_id": job_id, "source": source_id,
                "source_job_id": suffix, "source_url": "https://source.example.test/feed",
                "application_url": "https://jobs.example.test/apply/" + suffix,
                "raw": raw, "fetched_at": "2026-10-08T00:00:00Z",
            })

    @staticmethod
    def _request_criteria():
        return {"criteria": {
            "include_titles": ["Systems Administrator"],
            "exclude_titles": [],
            "required_keywords": ["Linux"],
            "excluded_keywords": [],
            "exact_phrases": [],
        }}

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            postings = [dict(row) for row in connection.execute(
                select(m.postings).order_by(m.postings.c.id)).mappings()]
            searches = [dict(row) for row in connection.execute(
                select(m.search_profiles).order_by(m.search_profiles.c.id)).mappings()]
            matches = [dict(row) for row in connection.execute(
                select(m.matches).order_by(m.matches.c.id)).mappings()]
            audit = [dict(row) for row in connection.execute(
                select(m.audit).order_by(m.audit.c.id)).mappings()]
        return postings, searches, matches, audit

    def test_preview_uses_selected_unscored_source_rows_and_reports_each_prefilter_stage(self):
        response = self.client.post(
            f"/profiles/fixture/search-profiles/{self.search_profile_id}/cheap-prefilter-preview",
            json=self._request_criteria(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "profile_id": "fixture",
            "search_profile_id": self.search_profile_id,
            "stage_counts": {"captured": 3, "prefiltered": 1},
            "candidate_count": 1,
            "rejected_count": 2,
            "deferred_count": 0,
        })
        self.assertNotIn("Finance", response.text)

    def test_invalid_or_cross_profile_search_cannot_read_candidates(self):
        missing = self.client.post(
            f"/profiles/missing/search-profiles/{self.search_profile_id}/cheap-prefilter-preview",
            json=self._request_criteria(),
        )
        self.assertEqual(missing.status_code, 404)
        cross_profile = self.client.post(
            f"/profiles/fixture/search-profiles/{self.other_search_profile_id}/cheap-prefilter-preview",
            json=self._request_criteria(),
        )
        self.assertEqual(cross_profile.status_code, 409)

    def test_preview_without_explicit_source_scope_fails_closed(self):
        response = self.client.post(
            f"/profiles/fixture/search-profiles/{self.no_source_search_profile_id}/cheap-prefilter-preview",
            json=self._request_criteria(),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "CHEAP_PREFILTER_SOURCES_REQUIRED")

    def test_preview_is_read_only_and_keeps_postings_and_saved_criteria_durable(self):
        before = self._snapshot()
        url = f"/profiles/fixture/search-profiles/{self.search_profile_id}/cheap-prefilter-preview"
        first = self.client.post(url, json=self._request_criteria())
        second = self.client.post(url, json=self._request_criteria())
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self._snapshot(), before)
        for private_source_value in ("https://source.example.test/feed", "https://jobs.example.test/apply/match"):
            self.assertNotIn(private_source_value, first.text)


if __name__ == "__main__":
    unittest.main()
