"""Fail-closed scope and no-write checks for cheap prefilter preview API."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.cheap_prefilter import create_cheap_prefilter_router
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class CheapPrefilterApiScopeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-cheap-prefilter-scope-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(root / 'jobs.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})

        self.fixture_source_id = configure_source(self.store, "scope-board", provider="greenhouse")
        self.other_source_id = configure_source(self.store, "private-board", provider="greenhouse")
        self.search_profile_id = self._save_search(
            "fixture-search", "fixture", [self.fixture_source_id],
        )
        self.cross_profile_search_id = self._save_search(
            "other-search", "other", [self.other_source_id],
        )
        self.empty_scope_search_id = self._save_search("empty-search", "fixture", [])
        self.unavailable_scope_search_id = self._save_search(
            "unavailable-search", "fixture", [self.fixture_source_id],
        )
        self.non_list_scope_search_id = self._save_search(
            "malformed-search", "fixture", [self.fixture_source_id],
        )
        with self.store.engine.begin() as connection:
            connection.execute(update(m.search_profiles).where(
                m.search_profiles.c.id == self.unavailable_scope_search_id,
            ).values(source_ids=["source-not-configured"]))
            connection.execute(update(m.search_profiles).where(
                m.search_profiles.c.id == self.non_list_scope_search_id,
            ).values(source_ids="fixture-source-as-scalar"))

        self._seed_private_posting()
        app = FastAPI()
        app.include_router(create_cheap_prefilter_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id, source_ids):
        return save_search_profile(self.store, {
            "id": search_id,
            "profile_id": profile_id,
            "name": search_id,
            "enabled": False,
            "include_titles": [],
            "exclude_titles": [],
            "required_keywords": [],
            "optional_keywords": [],
            "excluded_keywords": [],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": source_ids,
        })

    def _seed_private_posting(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "private-company", "name": "Private Scope Fixture", "domain": "",
            })
            upsert(connection, m.jobs, {
                "id": "private-job", "company_id": "private-company", "requisition": "private",
                "title": "Never disclose cross profile posting", "description": "private-description-token",
                "skills": [], "location": "Remote", "remote_type": "REMOTE",
            })
            upsert(connection, m.postings, {
                "id": "private-posting", "job_id": "private-job", "source": self.other_source_id,
                "source_job_id": "private", "source_url": "https://private.example.test/feed",
                "application_url": "https://private.example.test/apply", "raw": {
                    "title": "Never disclose cross profile posting",
                    "description": "private-description-token",
                }, "fetched_at": "2026-10-08T00:00:00Z",
            })

    @staticmethod
    def _request():
        return {"criteria": {"include_titles": [], "exclude_titles": [],
                             "required_keywords": [], "excluded_keywords": [], "exact_phrases": []}}

    def _url(self, profile_id, search_id):
        return f"/profiles/{profile_id}/search-profiles/{search_id}/cheap-prefilter-preview"

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

    def test_cross_profile_saved_search_is_rejected_without_disclosure_or_writes(self):
        before = self._snapshot()
        response = self.client.post(
            self._url("fixture", self.cross_profile_search_id), json=self._request(),
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "CHEAP_PREFILTER_SEARCH_PROFILE_PROFILE_MISMATCH")
        for private_value in (
            "Never disclose cross profile posting", "private-description-token",
            "https://private.example.test/feed", "https://private.example.test/apply",
        ):
            self.assertNotIn(private_value, response.text)
        self.assertEqual(self._snapshot(), before)

    def test_empty_unavailable_and_malformed_source_scopes_fail_closed_without_writes(self):
        cases = (
            (self.empty_scope_search_id, "CHEAP_PREFILTER_SOURCES_REQUIRED"),
            (self.unavailable_scope_search_id, "CHEAP_PREFILTER_SOURCES_UNAVAILABLE"),
            (self.non_list_scope_search_id, "CHEAP_PREFILTER_SOURCE_SCOPE_UNSUPPORTED"),
        )
        for search_id, expected_code in cases:
            with self.subTest(search_id=search_id):
                before = self._snapshot()
                response = self.client.post(
                    self._url("fixture", search_id), json=self._request(),
                )

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"]["code"], expected_code)
                self.assertNotIn("private-description-token", response.text)
                self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
