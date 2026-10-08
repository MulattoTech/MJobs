"""Profile-scoped, versioned API for configurable role-scoring profiles."""
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


class ScoringProfilesApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-scoring-profiles-api-")
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
        self._save_search("fixture-search", "fixture")
        self._save_search("fixture-search-two", "fixture")
        self._save_search("other-search", "other")
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "fixture-search",
            )).mappings().one()
            criteria = dict(row["criteria"])
            criteria["unrelated_marker"] = {"preserve": ["this", "value"]}
            connection.execute(m.search_profiles.update().where(
                m.search_profiles.c.id == "fixture-search",
            ).values(criteria=criteria))
        app = FastAPI()
        app.include_router(create_scoring_profiles_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_profile_id, profile_id):
        return save_search_profile(self.store, {
            "id": search_profile_id,
            "profile_id": profile_id,
            "name": f"Search {search_profile_id}",
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
            "source_ids": [],
        })

    def _url(self, profile_id="fixture", search_profile_id="fixture-search"):
        return (f"/profiles/{profile_id}/search-profiles/{search_profile_id}"
                "/role-scoring-profile")

    @staticmethod
    def _definition(role_family="User Defined Family", weights=None, thresholds=None):
        return {
            "schema_version": 1,
            "role_family": role_family,
            "weights": weights if weights is not None else {},
            "thresholds": thresholds if thresholds is not None else {},
        }

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in
                             connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_create_readback_and_family_resolution_are_scoped_and_explainable(self):
        before_read = self._snapshot()
        initial = self.client.get(self._url())
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json(), {"profile": None})
        self.assertEqual(self._snapshot(), before_read)

        definition = self._definition(
            "User Defined Family",
            {"skills": 40, "future_specialty_signal": 100},
            {"future_specialty_signal": 0},
        )
        before_create = self._snapshot()
        created = self.client.put(self._url(), json={
            "expected_version": None,
            "profile": definition,
        })
        self.assertEqual(created.status_code, 200, created.text)
        profile = created.json()["profile"]
        self.assertTrue(profile["id"])
        self.assertTrue(profile["version"])
        self.assertEqual(profile["role_family"], "User Defined Family")
        self.assertEqual(profile["weights"], {
            "skills": 40.0, "future_specialty_signal": 100.0,
        })
        self.assertEqual(profile["thresholds"], {"future_specialty_signal": 0.0})
        with self.store.engine.connect() as connection:
            saved = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "fixture-search",
            )).mappings().one()
        self.assertEqual(saved["criteria"]["unrelated_marker"],
                         {"preserve": ["this", "value"]})
        self.assertEqual(self.client.get(self._url()).json(), created.json())
        self.assertEqual(
            self.client.get(self._url(search_profile_id="fixture-search-two")).json(),
            {"profile": None},
        )
        after_create = self._snapshot()
        self.assertEqual(after_create[m.operations.name], before_create[m.operations.name])
        self.assertEqual(after_create[m.matches.name], before_create[m.matches.name])

        before_resolution = self._snapshot()
        matching_family = self.client.get(self._url(), params={
            "role_family": " user   defined family ",
        })
        self.assertEqual(matching_family.status_code, 200, matching_family.text)
        self.assertEqual(matching_family.json(), {
            "profile": profile,
            "resolution": {
                "requested_role_family": "user defined family",
                "selected_role_family": "User Defined Family",
                "profile_id": profile["id"],
                "profile_version": profile["version"],
                "selection": "ROLE_FAMILY",
            },
        })

        unknown_family = self.client.get(self._url(), params={"role_family": "new family"})
        self.assertEqual(unknown_family.status_code, 200, unknown_family.text)
        self.assertEqual(unknown_family.json(), {
            "profile": None,
            "resolution": {
                "requested_role_family": "new family",
                "selected_role_family": None,
                "profile_id": None,
                "profile_version": None,
                "selection": "LEGACY_DEFAULT",
            },
        })
        self.assertEqual(self._snapshot(), before_resolution)

    def test_update_requires_current_version_and_does_not_enqueue_rescoring(self):
        created = self.client.put(self._url(), json={
            "expected_version": None,
            "profile": self._definition("Family One", {"skills": 50}),
        })
        self.assertEqual(created.status_code, 200, created.text)
        first = created.json()["profile"]
        operations_before = self._snapshot()[m.operations.name]
        matches_before = self._snapshot()[m.matches.name]

        updated = self.client.put(self._url(), json={
            "expected_version": first["version"],
            "profile": self._definition("Family Two", {"new_signal": 75}),
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        second = updated.json()["profile"]
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(second["version"], first["version"])
        self.assertEqual(second["role_family"], "Family Two")
        after_update = self._snapshot()
        self.assertEqual(after_update[m.operations.name], operations_before)
        self.assertEqual(after_update[m.matches.name], matches_before)

        before_stale = self._snapshot()
        duplicate_create = self.client.put(self._url(), json={
            "expected_version": None,
            "profile": self._definition("Duplicate Create", {}),
        })
        self.assertEqual(duplicate_create.status_code, 409, duplicate_create.text)
        self.assertEqual(self._snapshot(), before_stale)

        stale = self.client.put(self._url(), json={
            "expected_version": first["version"],
            "profile": self._definition("Stale Family", {}),
        })
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self._snapshot(), before_stale)

    def test_bad_schema_versions_dimensions_and_numbers_are_rejected_without_writes(self):
        invalid_definitions = [
            self._definition("Family", {"": 5}),
            self._definition("Family", {"skills": -0.01}),
            self._definition("Family", {"skills": 100.01}),
            self._definition("Family", {"skills": True}),
            {**self._definition(), "schema_version": True},
            {**self._definition(), "schema_version": 2},
            {**self._definition(), "role_family": "   "},
            {**self._definition(), "unexpected": "not allowed"},
        ]
        before = self._snapshot()
        for definition in invalid_definitions:
            with self.subTest(definition=definition):
                response = self.client.put(self._url(), json={
                    "expected_version": None, "profile": definition,
                })
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self._snapshot(), before)

        nonfinite = self.client.put(
            self._url(),
            content=(b'{"expected_version":null,"profile":{"schema_version":1,'
                     b'"role_family":"Family","weights":{"signal":Infinity},'
                     b'"thresholds":{}}}'),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(nonfinite.status_code, 422, nonfinite.text)
        self.assertEqual(self._snapshot(), before)

    def test_foreign_or_missing_scope_and_version_envelopes_are_rejected_without_writes(self):
        before = self._snapshot()
        cases = (
            ("missing", "fixture-search", 404),
            ("fixture", "missing-search", 404),
            ("fixture", "other-search", 409),
        )
        for profile_id, search_profile_id, expected_status in cases:
            with self.subTest(profile_id=profile_id, search_profile_id=search_profile_id):
                response = self.client.get(self._url(profile_id, search_profile_id))
                self.assertEqual(response.status_code, expected_status, response.text)

                rejected_write = self.client.put(
                    self._url(profile_id, search_profile_id),
                    json={"expected_version": None,
                          "profile": self._definition("No Write", {})},
                )
                self.assertEqual(rejected_write.status_code, expected_status,
                                 rejected_write.text)
                self.assertEqual(self._snapshot(), before)

        invalid_envelopes = [
            {"profile": self._definition()},
            {"expected_version": True, "profile": self._definition()},
            {"expected_version": " ", "profile": self._definition()},
            {"expected_version": None, "profile": self._definition(), "extra": 1},
        ]
        for value in invalid_envelopes:
            with self.subTest(value=value):
                response = self.client.put(self._url(), json=value)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self._snapshot(), before)

    def test_corrupt_stored_profile_returns_stable_error_without_repair(self):
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "fixture-search",
            )).mappings().one()
            criteria = dict(row["criteria"])
            criteria["role_scoring_profile"] = {
                "id": "stored-id",
                "version": "stored-version",
                "schema_version": True,
                "role_family": "Broken",
                "weights": {},
                "thresholds": {},
            }
            connection.execute(m.search_profiles.update().where(
                m.search_profiles.c.id == "fixture-search",
            ).values(criteria=criteria))
        before_read = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"],
                         "SCORING_PROFILE_STORED_CONFIG_INVALID")
        self.assertEqual(self._snapshot(), before_read)


if __name__ == "__main__":
    unittest.main()
