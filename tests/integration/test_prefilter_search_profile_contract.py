"""Connected API contract for applying persisted search criteria to previews."""
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


class PrefilterSearchProfileContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-prefilter-profile-contract-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(root / 'jobs.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.source_id = configure_source(self.store, "profile-contract-board", provider="greenhouse")
        self.search_profile_id = save_search_profile(self.store, {
            "id": "profile-contract-search",
            "profile_id": "fixture",
            "name": "Profile contract search",
            "enabled": False,
            "include_titles": ["Systems Administrator"],
            "exclude_titles": [],
            "required_keywords": ["Linux"],
            "optional_keywords": [],
            "excluded_keywords": [],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [self.source_id],
        })
        self._seed_postings()
        app = FastAPI()
        app.include_router(create_cheap_prefilter_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_postings(self):
        values = (
            ("linux-admin", "Systems Administrator", "Linux server operations"),
            ("windows-admin", "Systems Administrator", "Windows server operations"),
            ("windows-accountant", "Accountant", "Windows finance systems"),
        )
        with self.store.engine.begin() as connection:
            for suffix, title, description in values:
                company_id = f"company-{suffix}"
                job_id = f"job-{suffix}"
                upsert(connection, m.companies, {
                    "id": company_id, "name": f"Synthetic {suffix}", "domain": "",
                })
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": company_id, "requisition": suffix,
                    "title": title, "description": description, "skills": [],
                    "location": "Remote", "remote_type": "REMOTE",
                })
                upsert(connection, m.postings, {
                    "id": f"posting-{suffix}", "job_id": job_id, "source": self.source_id,
                    "source_job_id": suffix, "source_url": "https://source.example.test/feed",
                    "application_url": f"https://jobs.example.test/apply/{suffix}",
                    "raw": {"title": title, "description": description},
                    "fetched_at": "2026-10-08T00:00:00Z",
                })

    def _url(self):
        return (
            f"/profiles/fixture/search-profiles/{self.search_profile_id}"
            "/cheap-prefilter-preview"
        )

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return tuple(
                [dict(row) for row in connection.execute(select(table).order_by(*order)).mappings()]
                for table, order in (
                    (m.postings, (m.postings.c.id,)),
                    (m.search_profiles, (m.search_profiles.c.id,)),
                    (m.matches, (m.matches.c.id,)),
                    (m.audit, (m.audit.c.id,)),
                )
            )

    def test_empty_preview_criteria_applies_saved_title_and_keyword_filters(self):
        before = self._snapshot()

        response = self.client.post(self._url(), json={"criteria": {}})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stage_counts"], {"captured": 3, "prefiltered": 1})
        self.assertEqual(response.json()["candidate_count"], 1)
        self.assertEqual(response.json()["rejected_count"], 2)
        self.assertEqual(self._snapshot(), before)

    def test_explicit_preview_keyword_override_preserves_saved_title_filter(self):
        before = self._snapshot()

        response = self.client.post(
            self._url(), json={"criteria": {"required_keywords": ["Windows"]}},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stage_counts"], {"captured": 3, "prefiltered": 1})
        self.assertEqual(response.json()["candidate_count"], 1)
        self.assertEqual(response.json()["rejected_count"], 2)
        self.assertEqual(self._snapshot(), before)

    def test_invalid_preview_terms_fail_without_mutating_saved_search_or_postings(self):
        before = self._snapshot()

        response = self.client.post(
            self._url(), json={"criteria": {"include_titles": ["   "]}},
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
