"""API persistence checks for data-driven custom scoring dimensions."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.scoring_profiles import create_scoring_profiles_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE


class CustomScoringDimensionsApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-custom-scoring-api-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'profiles.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        self._save_search("selected-search", "fixture", ["Linux"])
        self._save_search("other-search", "fixture", ["Windows"])
        self._save_search("foreign-search", "other", ["macOS"])

        app = FastAPI()
        app.include_router(create_scoring_profiles_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id, required_keywords):
        return save_search_profile(self.store, {
            "id": search_id,
            "profile_id": profile_id,
            "name": f"Search {search_id}",
            "enabled": False,
            "include_titles": ["Systems Administrator"],
            "exclude_titles": [],
            "required_keywords": required_keywords,
            "optional_keywords": [],
            "excluded_keywords": [],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [],
        })

    @staticmethod
    def _url(profile_id="fixture", search_id="selected-search"):
        return (f"/profiles/{profile_id}/search-profiles/{search_id}"
                "/role-scoring-profile")

    @staticmethod
    def _profile(weights, thresholds):
        return {
            "schema_version": 1,
            "role_family": "Infrastructure Engineering",
            "weights": weights,
            "thresholds": thresholds,
        }

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in
                             connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_custom_dimensions_save_and_readback_stay_with_selected_search(self):
        before = self._snapshot()
        response = self.client.put(self._url(), json={
            "expected_version": None,
            "profile": self._profile(
                {"skills": 70, "organization_transparency": 30},
                {"skills": 60, "organization_transparency": 45},
            ),
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["search_profile_id"], "selected-search")
        self.assertEqual(body["profile"]["weights"], {
            "skills": 70.0, "organization_transparency": 30.0,
        })
        self.assertEqual(body["profile"]["thresholds"], {
            "skills": 60.0, "organization_transparency": 45.0,
        })

        readback = self.client.get(self._url())
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json(), body)
        self.assertEqual(self.client.get(self._url(search_id="other-search")).json(), {
            "profile_id": "fixture",
            "search_profile_id": "other-search",
            "profile": None,
        })

        with self.store.engine.connect() as connection:
            selected = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one()
            second = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "other-search",
            )).mappings().one()
        selected_before = next(
            row for row in before[m.search_profiles.name]
            if row["id"] == "selected-search"
        )
        expected_criteria = dict(selected_before["criteria"])
        expected_criteria["role_scoring_profile"] = body["profile"]
        self.assertEqual(selected["criteria"], expected_criteria)
        self.assertEqual(second["criteria"], next(
            row["criteria"] for row in before[m.search_profiles.name]
            if row["id"] == "other-search"
        ))
        after = self._snapshot()
        self.assertEqual(after[m.matches.name], before[m.matches.name])
        self.assertEqual(after[m.operations.name], before[m.operations.name])

        before_foreign = self._snapshot()
        foreign = self.client.put(self._url("fixture", "foreign-search"), json={
            "expected_version": None,
            "profile": self._profile({"organization_transparency": 100}, {}),
        })
        self.assertEqual(foreign.status_code, 409, foreign.text)
        self.assertEqual(self._snapshot(), before_foreign)

    def test_malformed_custom_dimension_keys_are_rejected_without_state_changes(self):
        before = self._snapshot()
        malformed_profiles = (
            self._profile({"": 25}, {}),
            self._profile({"   ": 25}, {}),
            self._profile({"x" * 101: 25}, {}),
        )
        for profile in malformed_profiles:
            with self.subTest(profile=profile):
                response = self.client.put(self._url(), json={
                    "expected_version": None,
                    "profile": profile,
                })
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
