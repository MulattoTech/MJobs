"""Connected-app acceptance for saved-search scoring profile routers."""

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


class ScoringProfileRouterMountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-scoring-router-mount-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'app.sqlite').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        self._save_search("selected-search", "fixture")
        self._save_search("second-search", "fixture")
        self._save_search("other-search", "other")
        self._seed_match()

        app = create_app(
            self.store, "http://127.0.0.1:1", self.root,
            operation_mode="PERSONAL",
        )
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id):
        return save_search_profile(self.store, {
            "id": search_id,
            "profile_id": profile_id,
            "name": f"Search {search_id}",
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

    def _seed_match(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="mount-company", name="Mount Fixture Co.", domain="fixture.test",
            ))
            connection.execute(m.jobs.insert().values(
                id="mount-job", company_id="mount-company", requisition="mount-req",
                title="Systems Administrator", description="Maintain systems.",
                skills=["Linux"], location="Remote", remote_type="REMOTE", attributes={},
            ))

    @staticmethod
    def _profile_url(profile_id="fixture", search_id="selected-search"):
        return (f"/profiles/{profile_id}/search-profiles/{search_id}"
                "/role-scoring-profile")

    @staticmethod
    def _explanation_url(profile_id="fixture", search_id="selected-search", job_id="mount-job"):
        return (f"/profiles/{profile_id}/search-profiles/{search_id}/jobs/{job_id}"
                "/match-explanation")

    @staticmethod
    def _profile_definition(weights):
        return {
            "schema_version": 1,
            "role_family": "Systems Administration",
            "weights": weights,
            "thresholds": {},
        }

    def _persist_current_match_explanation(self, profile):
        career = self.store.profile("fixture")
        contribution = {
            "dimension": "skills",
            "weight": profile["weights"]["skills"],
            "raw_score": 73.0,
            "weighted_contribution": 73.0,
            "explanation": "The stored deterministic skill score contributes to this advisory match.",
            "provenance": {"source": "LEGACY_MATCH_BASELINE"},
        }
        components = {
            "scoring_profile": {
                "requested_role_family": "systems administration",
                "selected_role_family": profile["role_family"],
                "profile_id": profile["id"],
                "profile_version": profile["version"],
                "selection": "ROLE_FAMILY",
            },
            "scoring_contributions": [contribution],
        }
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.insert().values(
                id="mount-match", job_id="mount-job", profile_id="fixture",
                profile_version=career["version"], score=73.0, missing=[],
                components=components,
            ))

    def _saved_search_and_match_snapshot(self):
        with self.store.engine.connect() as connection:
            searches = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id.in_(
                    ["selected-search", "second-search", "other-search"],
                ),
            )).mappings().all()
            matches = connection.execute(select(m.matches)).mappings().all()
        return {
            "searches": [dict(row) for row in searches],
            "matches": [dict(row) for row in matches],
        }

    def test_create_app_mounts_scoped_versioned_profiles_and_read_only_explanations(self):
        initial = self.client.get(self._profile_url())
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json(), {
            "profile_id": "fixture", "search_profile_id": "selected-search",
            "profile": None,
        })

        created = self.client.put(self._profile_url(), json={
            "expected_version": None,
            "profile": self._profile_definition({"skills": 100}),
        })
        self.assertEqual(created.status_code, 200, created.text)
        first = created.json()["profile"]
        self.assertTrue(first["id"])
        self.assertTrue(first["version"])
        self.assertEqual(self.client.get(self._profile_url()).json(), {
            "profile_id": "fixture", "search_profile_id": "selected-search",
            "profile": first,
        })
        self.assertEqual(
            self.client.get(self._profile_url(search_id="second-search")).json(),
            {"profile_id": "fixture", "search_profile_id": "second-search", "profile": None},
        )

        updated = self.client.put(self._profile_url(), json={
            "expected_version": first["version"],
            "profile": self._profile_definition({"skills": 75}),
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        current = updated.json()["profile"]
        self.assertEqual(current["id"], first["id"])
        self.assertNotEqual(current["version"], first["version"])
        self.assertEqual(current["weights"], {"skills": 75.0})
        self.assertEqual(self.client.get(self._profile_url()).json(), {
            "profile_id": "fixture", "search_profile_id": "selected-search",
            "profile": current,
        })

        self._persist_current_match_explanation(current)
        explanation = self.client.get(self._explanation_url())
        self.assertEqual(explanation.status_code, 200, explanation.text)
        body = explanation.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["search_profile_id"], "selected-search")
        self.assertEqual(body["job_id"], "mount-job")
        self.assertEqual(body["score"], 73.0)
        self.assertEqual(body["resolution"], "CURRENT")
        self.assertEqual(body["scoring_profile"], {
            "id": current["id"],
            "version": current["version"],
            "role_family": "Systems Administration",
        })
        self.assertEqual(body["scoring_contributions"][0]["provenance"], {
            "source": "LEGACY_MATCH_BASELINE",
        })

        before_foreign_read = self._saved_search_and_match_snapshot()
        foreign_search = self.client.get(self._explanation_url(search_id="other-search"))
        self.assertEqual(foreign_search.status_code, 404, foreign_search.text)
        self.assertEqual(foreign_search.json()["detail"]["code"],
                         "MATCH_EXPLANATION_SEARCH_NOT_FOUND")
        self.assertEqual(self._saved_search_and_match_snapshot(), before_foreign_read)
        self.assertEqual(
            self.client.get(self._profile_url(search_id="second-search")).json(),
            {"profile_id": "fixture", "search_profile_id": "second-search", "profile": None},
        )


if __name__ == "__main__":
    unittest.main()
