"""Connected saved-search title add/readback and non-application side effects."""
from copy import deepcopy
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, identity, upsert
from services.discovery.search_profiles import save_search_profile
from services.discovery.sources.collect import configure_source
from tests.integration.test_spine import PROFILE


class SearchProfileSuggestionsSavedSearchScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-suggestion-saved-search-scope-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Fixture",
                                 "email": "other@example.test"})
        self.source_id = configure_source(
            self.store, "saved-search-scope-fixture", enabled=False,
        )
        self.profile_version = self.store.profile("fixture")["version"]
        self.other_version = self.store.profile("other")["version"]
        self.job_filters = {
            "q": "",
            "remote_type": "REMOTE",
            "provider": "",
            "sort": "score",
            "application_pipeline_status": "READY",
            "application_capability": "REVIEW_REQUIRED",
        }
        self._seed_searches_matches_and_application()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def tearDown(self):
        self.client.close()

    def _search_data(self, identifier, profile_id, titles):
        return {
            "id": identifier,
            "profile_id": profile_id,
            "name": f"{profile_id} search",
            "enabled": False,
            "include_titles": titles,
            "exclude_titles": ["Intern"],
            "required_keywords": ["Linux"],
            "remote": "REQUIRED",
            "employment_types": ["FULL_TIME"],
            "source_ids": [self.source_id],
            "job_filters": self.job_filters,
            "scheduler": {"enabled": False, "interval_seconds": 3600},
        }

    def _seed_searches_matches_and_application(self):
        save_search_profile(self.store, self._search_data(
            "fixture-search", "fixture", ["System Administrator"],
        ))
        save_search_profile(self.store, self._search_data(
            "other-search", "other", ["Platform Administrator"],
        ))
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "suggestion-add-company", "name": "Synthetic Employer", "domain": "",
            })
            rows = (
                ("suggestion-current", "System Administrator", "fixture", self.profile_version, 95.0),
                ("suggestion-added-title", "IT Operations Engineer", "fixture", self.profile_version, 91.0),
                ("suggestion-remaining-title", "Network Administrator", "fixture", self.profile_version, 88.0),
                ("suggestion-other-title", "Platform Administrator", "other", self.other_version, 94.0),
            )
            for job_id, title, profile_id, version, score in rows:
                upsert(connection, m.jobs, {
                    "id": job_id,
                    "company_id": "suggestion-add-company",
                    "requisition": job_id,
                    "title": title,
                    "description": "Synthetic saved-search suggestions fixture.",
                    "skills": [],
                    "location": "Remote",
                    "remote_type": "REMOTE",
                    "attributes": {},
                })
                connection.execute(insert(m.matches).values(
                    id=identity(job_id, profile_id, version),
                    job_id=job_id,
                    profile_id=profile_id,
                    profile_version=version,
                    score=score,
                    missing=[],
                    components={},
                ))
            upsert(connection, m.routes, {
                "id": "suggestion-add-application-route",
                "job_id": "suggestion-current",
                "company_id": "suggestion-add-company",
                "provider": "manual",
                "tenant": "fixture",
                "requisition": "suggestion-current",
                "url": "https://jobs.example.invalid/suggestion-current",
            })
            upsert(connection, m.applications, {
                "id": "suggestion-add-existing-application",
                "job_id": "suggestion-current",
                "route_id": "suggestion-add-application-route",
                "profile_id": "fixture",
                "profile_version": self.profile_version,
                "state": "PREPARED",
                "target": "MOCK",
                "fact_ids": [],
                "trace": [],
                "document_version": 1,
                "confirmation": None,
                "created_at": "2026-10-09T00:00:00Z",
                "submitted_at": None,
            })

    def _get_without_network(self, url):
        with patch.object(socket, "create_connection",
                          side_effect=AssertionError("network access attempted")), \
                patch("urllib.request.urlopen",
                      side_effect=AssertionError("network access attempted")):
            return self.client.get(url)

    def _put_without_network(self, url, *, json):
        with patch.object(socket, "create_connection",
                          side_effect=AssertionError("network access attempted")), \
                patch("urllib.request.urlopen",
                      side_effect=AssertionError("network access attempted")):
            return self.client.put(url, json=json)

    def _snapshot(self, *tables):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(
                    select(table).order_by(table.c.id),
                ).mappings()]
                for table in tables
            }

    def test_explicit_title_add_persists_readback_and_preserves_search_and_application_criteria(self):
        application_tables = (
            m.applications, m.attempts, m.operations, m.documents,
            m.application_route_choices,
        )
        before_application_state = self._snapshot(*application_tables)
        before_other_search = self._snapshot(m.search_profiles)["search_profiles"]
        before_other_search = next(
            (row for row in before_other_search if row["id"] == "other-search"),
        )

        add = self._put_without_network("/search-profiles", json={
            "id": "fixture-search",
            "profile_id": "fixture",
            "name": "fixture search",
            "include_titles": [
                "System Administrator", " IT Operations Engineer ", "system administrator",
            ],
        })

        self.assertEqual(add.status_code, 200, add.text)
        self.assertEqual(add.json(), {"id": "fixture-search"})
        readback = self._get_without_network("/search-profiles/fixture-search")
        self.assertEqual(readback.status_code, 200, readback.text)
        saved = readback.json()
        self.assertEqual(saved["profile_id"], "fixture")
        self.assertEqual(saved["criteria"]["include_titles"], [
            "System Administrator", "IT Operations Engineer",
        ])
        self.assertEqual(saved["criteria"]["exclude_titles"], ["Intern"])
        self.assertEqual(saved["criteria"]["required_keywords"], ["Linux"])
        self.assertEqual(saved["criteria"]["remote"], "REQUIRED")
        self.assertEqual(saved["criteria"]["employment_types"], ["FULL_TIME"])
        for key, value in self.job_filters.items():
            self.assertEqual(saved["criteria"]["job_filters"][key], value)
        self.assertEqual(saved["criteria"]["scheduler"], {
            "enabled": False, "interval_seconds": 3600,
        })
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["source_ids"], [self.source_id])

        fixture_searches = self._get_without_network(
            "/search-profiles?profile_id=fixture",
        )
        other_searches = self._get_without_network(
            "/search-profiles?profile_id=other",
        )
        self.assertEqual(fixture_searches.status_code, 200, fixture_searches.text)
        self.assertEqual(other_searches.status_code, 200, other_searches.text)
        self.assertEqual([row["id"] for row in fixture_searches.json()["search_profiles"]], [
            "fixture-search",
        ])
        self.assertEqual([row["id"] for row in other_searches.json()["search_profiles"]], [
            "other-search",
        ])
        self.assertTrue(all(row["profile_id"] == "fixture"
                            for row in fixture_searches.json()["search_profiles"]))
        self.assertTrue(all(row["profile_id"] == "other"
                            for row in other_searches.json()["search_profiles"]))

        other_readback = self._get_without_network("/search-profiles/other-search")
        self.assertEqual(other_readback.status_code, 200, other_readback.text)
        self.assertEqual(other_readback.json(), before_other_search)
        suggestions = self._get_without_network(
            "/profiles/fixture/search-profile-suggestions?search_profile_id=fixture-search",
        )
        self.assertEqual(suggestions.status_code, 200, suggestions.text)
        self.assertEqual([item["title"] for item in suggestions.json()["suggestions"]], [
            "Network Administrator",
        ])
        self.assertEqual(self._snapshot(*application_tables), before_application_state)
        application = self._snapshot(m.applications)["applications"][0]
        self.assertEqual(application["state"], "PREPARED")
        self.assertIsNone(application["submitted_at"])

    def test_invalid_title_and_foreign_profile_update_do_not_change_saved_or_application_state(self):
        watched_tables = (
            m.search_profiles, m.applications, m.attempts, m.operations,
            m.documents, m.application_route_choices,
        )
        before = self._snapshot(*watched_tables)
        invalid_title = self._put_without_network("/search-profiles", json={
            "id": "fixture-search",
            "profile_id": "fixture",
            "name": "fixture search",
            "include_titles": ["   "],
        })
        foreign_owner = self._put_without_network("/search-profiles", json={
            "id": "fixture-search",
            "profile_id": "other",
            "name": "other search",
            "include_titles": ["Unapproved Role"],
        })

        self.assertEqual(invalid_title.status_code, 422, invalid_title.text)
        self.assertEqual(foreign_owner.status_code, 409, foreign_owner.text)
        self.assertEqual(self._snapshot(*watched_tables), before)

    def test_foreign_and_stale_suggestion_search_ids_do_not_leak_or_change_applications(self):
        watched_tables = (
            m.search_profiles, m.matches, m.applications, m.attempts,
            m.operations, m.documents, m.application_route_choices,
        )
        before = self._snapshot(*watched_tables)

        for search_profile_id in ("other-search", "deleted-search"):
            with self.subTest(search_profile_id=search_profile_id):
                response = self._get_without_network(
                    "/profiles/fixture/search-profile-suggestions?search_profile_id="
                    + search_profile_id,
                )
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["detail"], {
                    "code": "SEARCH_PROFILE_SUGGESTIONS_SEARCH_NOT_FOUND",
                })
        self.assertEqual(self._snapshot(*watched_tables), before)


if __name__ == "__main__":
    unittest.main()
