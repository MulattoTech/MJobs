"""Current contribution weights are readable; stale attribution fails closed."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE


class MatchingExplanationStaleContextRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-explanation-context-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "context.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        self._save_search("selected-search", "fixture")
        self._save_search("other-search", "other")
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="context-company", name="Context Fixture Co.", domain="context.test",
            ))
            connection.execute(m.jobs.insert().values(
                id="context-job", company_id="context-company", requisition="context-req",
                title="Systems Administrator", description="Synthetic persisted match.",
                skills=["Linux"], location="Remote", remote_type="REMOTE", attributes={},
            ))

        app = create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="PERSONAL",
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id):
        return save_search_profile(self.store, {
            "id": search_id, "profile_id": profile_id, "name": search_id,
            "enabled": False, "include_titles": [], "exclude_titles": [],
            "required_keywords": [], "optional_keywords": [], "excluded_keywords": [],
            "exact_phrases": [], "technology_aliases": {}, "remote": "ANY",
            "employment_types": [], "source_ids": [],
        })

    @staticmethod
    def _profile_url():
        return "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"

    @staticmethod
    def _explanation_url(search_id="selected-search"):
        return (f"/profiles/fixture/search-profiles/{search_id}/jobs/context-job/"
                "match-explanation")

    def _save_scoring_profile(self, weights):
        response = self.client.put(self._profile_url(), json={
            "expected_version": None,
            "profile": {
                "schema_version": 1,
                "role_family": "Systems Administration",
                "weights": weights,
                "thresholds": {},
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["profile"]

    def _insert_match(self, profile, *, contribution_weight, attributed_version=None):
        career = self.store.profile("fixture")
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.insert().values(
                id="context-match", job_id="context-job", profile_id="fixture",
                profile_version=career["version"], score=73.0, missing=[],
                components={
                    "scoring_profile": {
                        "requested_role_family": "systems administration",
                        "selected_role_family": profile["role_family"],
                        "profile_id": profile["id"],
                        "profile_version": attributed_version or profile["version"],
                        "selection": "ROLE_FAMILY",
                    },
                    "scoring_contributions": [{
                        "dimension": "skills",
                        "weight": float(contribution_weight),
                        "raw_score": 73.0,
                        "weighted_contribution": 73.0,
                        "explanation": "Stored deterministic skills baseline.",
                        "provenance": {"source": "LEGACY_MATCH_BASELINE"},
                    }],
                },
            ))

    def _database_bytes(self):
        return self.database.read_bytes()

    def test_current_profile_weight_and_matching_contribution_are_readable(self):
        current = self._save_scoring_profile({"skills": 75})
        self._insert_match(current, contribution_weight=75)
        before = self._database_bytes()

        response = self.client.get(self._explanation_url())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["scoring_profile"], {
            "id": current["id"], "version": current["version"],
            "role_family": "Systems Administration",
        })
        self.assertEqual(body["scoring_contributions"][0]["weight"], 75.0)
        self.assertEqual(body["scoring_contributions"][0]["provenance"], {
            "source": "LEGACY_MATCH_BASELINE",
        })
        self.assertEqual(self._database_bytes(), before)

    def test_old_default_weight_is_stale_against_configured_current_weight(self):
        current = self._save_scoring_profile({"skills": 75})
        # This reproduces the mount test's old fixture: the explanation claims 100
        # although the currently selected search configures the skills weight as 75.
        self._insert_match(current, contribution_weight=100)
        before = self._database_bytes()

        response = self.client.get(self._explanation_url())

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "MATCH_EXPLANATION_STALE_CONTEXT")
        self.assertEqual(self._database_bytes(), before)

    def test_stale_profile_attribution_and_foreign_search_remain_fail_closed(self):
        current = self._save_scoring_profile({"skills": 75})
        self._insert_match(current, contribution_weight=75, attributed_version="obsolete-version")
        before = self._database_bytes()

        stale = self.client.get(self._explanation_url())
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["detail"]["code"], "MATCH_EXPLANATION_STALE_CONTEXT")
        self.assertEqual(self._database_bytes(), before)

        foreign = self.client.get(self._explanation_url(search_id="other-search"))
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertEqual(foreign.json()["detail"]["code"], "MATCH_EXPLANATION_SEARCH_NOT_FOUND")
        self.assertEqual(self._database_bytes(), before)


if __name__ == "__main__":
    unittest.main()
