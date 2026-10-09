"""Integration contract for saved-search role scoring and match readback.

The profile and explanation routers are exercised against a real SQLite Store.
The current base does not yet mount these routers in create_app or have the
selected-search recalculate consumer; that production wiring is owned by root.
This test covers the stable router/composer/persisted-readback contract without
pretending that the consumer has run automatically.
"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.matching_explanations import create_matching_explanations_router
from apps.api.scoring_profiles import create_scoring_profiles_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.matching import build_explanation
from services.discovery.search_profiles import save_search_profile
from services.discovery.search_semantics import evaluate_search
from services.matching.profile_scoring import compose_profile_scoring
from services.matching.scoring_profiles import resolve_role_scoring_profile
from tests.integration.test_spine import PROFILE


class RoleScoringSearchFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-role-scoring-flow-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'role-scoring.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = deepcopy(PROFILE)
        # This absent fact must remain explicit as unsupported evidence after scoring.
        profile["facts"] = [
            fact for fact in profile.get("facts", [])
            if "kubernetes" not in str(fact.get("text", "")).casefold()
        ]
        self.store.save_profile(profile)
        self.profile = self.store.profile("fixture")
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        self._save_search("selected-search", "fixture")
        self._save_search("second-search", "fixture")
        self._save_search("other-search", "other")
        self._seed_jobs()

        app = FastAPI()
        app.include_router(create_scoring_profiles_router(self.store))
        app.include_router(create_matching_explanations_router(self.store))
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
            "required_keywords": ["Linux"],
            "optional_keywords": [],
            "excluded_keywords": ["temporary contract"],
            "exact_phrases": [],
            "technology_aliases": {},
            "remote": "ANY",
            "employment_types": [],
            "source_ids": [],
        })

    def _seed_jobs(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Example Infrastructure", domain="example.test",
            ))
            connection.execute(m.jobs.insert().values(
                id="scoring-job", company_id="company", requisition="req-1",
                title="Systems Administrator", description="Linux systems operations",
                skills=["Kubernetes"], location="Remote", remote_type="REMOTE",
                attributes={"specialized_signals": {"ai_intensity": {
                    "score": 80,
                    "provenance": {"source": "job_metadata", "evidence_ref": "ai-signal-1"},
                }}},
            ))
            connection.execute(m.jobs.insert().values(
                id="excluded-job", company_id="company", requisition="req-2",
                title="Temporary Contract Systems Administrator",
                description="Linux systems operations", skills=["Kubernetes"],
                location="Remote", remote_type="REMOTE", attributes={},
            ))

    @staticmethod
    def _definition(weights):
        return {
            "schema_version": 1,
            "role_family": "Systems Administration",
            "weights": weights,
            "thresholds": {},
        }

    def _profile_url(self, profile_id="fixture", search_id="selected-search"):
        return (f"/profiles/{profile_id}/search-profiles/{search_id}"
                "/role-scoring-profile")

    def _explanation_url(self, profile_id="fixture", search_id="selected-search",
                         job_id="scoring-job"):
        return (f"/profiles/{profile_id}/search-profiles/{search_id}/jobs/{job_id}"
                "/match-explanation")

    def _search(self, search_id="selected-search"):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == search_id,
            )).mappings().one())

    def _job(self, job_id="scoring-job"):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.jobs).where(
                m.jobs.c.id == job_id,
            )).mappings().one())

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in
                             connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def _materialize_profiled_match(self, saved_profile):
        """Persist the real composer result at the currently unmounted consumer seam."""
        job = self._job()
        legacy = build_explanation(job, self.profile)
        resolution = resolve_role_scoring_profile(
            saved_profile["role_family"],
            {saved_profile["role_family"]: saved_profile},
        )
        composed = compose_profile_scoring(job, legacy, resolution)
        components = {
            **legacy["components"],
            "scoring_profile": composed["scoring_profile"],
            "scoring_contributions": composed["scoring_contributions"],
        }
        with self.store.engine.begin() as connection:
            existing = connection.execute(select(m.matches.c.id).where(
                m.matches.c.job_id == "scoring-job",
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.profile["version"],
            )).scalar_one_or_none()
            values = {
                "score": composed["score"],
                "missing": legacy["missing"],
                "components": components,
            }
            if existing is None:
                connection.execute(m.matches.insert().values(
                    id="scoring-match", job_id="scoring-job", profile_id="fixture",
                    profile_version=self.profile["version"], **values,
                ))
            else:
                connection.execute(m.matches.update().where(
                    m.matches.c.id == existing,
                ).values(**values))
        return legacy, composed

    def test_saved_search_weights_explain_persisted_score_without_changing_hard_filters(self):
        search_before = self._search()
        criteria_before = deepcopy(search_before["criteria"])
        eligible_before = {
            job_id: evaluate_search(criteria_before, self._job(job_id))["matched"]
            for job_id in ("scoring-job", "excluded-job")
        }
        self.assertEqual(eligible_before, {"scoring-job": True, "excluded-job": False})
        self.assertEqual(self.client.get(self._profile_url()).json(), {"profile": None})
        self.assertEqual(self.client.get(self._profile_url(search_id="second-search")).json(),
                         {"profile": None})

        first_response = self.client.put(self._profile_url(), json={
            "expected_version": None,
            "profile": self._definition({
                "skills": 70, "ai_intensity": 30, "cloud_experience": 20,
            }),
        })
        self.assertEqual(first_response.status_code, 200, first_response.text)
        first = first_response.json()["profile"]
        self.assertEqual(self.client.get(self._profile_url()).json(), {"profile": first})
        self.assertEqual(self.client.get(
            self._profile_url(search_id="second-search")).json(), {"profile": None})

        legacy_one, composed_one = self._materialize_profiled_match(first)
        self.assertEqual(legacy_one["components"]["requirements"][0]["profile_evidence"],
                         "NO_APPROVED_EVIDENCE")
        self.assertIn("No approved career fact", legacy_one["components"]["requirements"][0]["explanation"])
        explanation_one = self.client.get(self._explanation_url())
        self.assertEqual(explanation_one.status_code, 200, explanation_one.text)
        body_one = explanation_one.json()
        self.assertEqual(body_one["profile_id"], "fixture")
        self.assertEqual(body_one["search_profile_id"], "selected-search")
        self.assertEqual(body_one["scoring_profile"], {
            "id": first["id"], "version": first["version"],
            "role_family": "Systems Administration",
        })
        by_dimension_one = {row["dimension"]: row
                            for row in body_one["scoring_contributions"]}
        self.assertEqual(by_dimension_one["ai_intensity"]["provenance"], {
            "source": "job_metadata", "evidence_ref": "ai-signal-1",
        })
        self.assertIsNone(by_dimension_one["cloud_experience"]["raw_score"])
        self.assertIsNone(by_dimension_one["cloud_experience"]["weighted_contribution"])
        self.assertEqual(by_dimension_one["cloud_experience"]["provenance"], {"source": "UNKNOWN"})
        self.assertEqual(body_one["score"], composed_one["score"])

        criteria_after_first = self._search()["criteria"]
        self.assertEqual({key: value for key, value in criteria_after_first.items()
                          if key != "role_scoring_profile"}, criteria_before)
        eligible_after_first = {
            job_id: evaluate_search(criteria_after_first, self._job(job_id))["matched"]
            for job_id in ("scoring-job", "excluded-job")
        }
        self.assertEqual(eligible_after_first, eligible_before)

        second_response = self.client.put(self._profile_url(), json={
            "expected_version": first["version"],
            "profile": self._definition({
                "skills": 20, "ai_intensity": 80, "cloud_experience": 20,
            }),
        })
        self.assertEqual(second_response.status_code, 200, second_response.text)
        second = second_response.json()["profile"]
        self.assertEqual(second["id"], first["id"])
        self.assertNotEqual(second["version"], first["version"])
        legacy_two, composed_two = self._materialize_profiled_match(second)
        self.assertEqual(legacy_two["score"], legacy_one["score"])
        self.assertEqual(self._job()["skills"], ["Kubernetes"])
        body_two_response = self.client.get(self._explanation_url())
        self.assertEqual(body_two_response.status_code, 200, body_two_response.text)
        body_two = body_two_response.json()
        self.assertEqual(body_two["scoring_profile"]["version"], second["version"])
        self.assertEqual(body_two["score"], composed_two["score"])
        self.assertNotEqual(body_two["score"], body_one["score"])
        self.assertEqual({key: value for key, value in self._search()["criteria"].items()
                          if key != "role_scoring_profile"}, criteria_before)
        eligible_after_second = {
            job_id: evaluate_search(self._search()["criteria"], self._job(job_id))["matched"]
            for job_id in ("scoring-job", "excluded-job")
        }
        self.assertEqual(eligible_after_second, eligible_before)

    def test_invalid_stale_and_foreign_scopes_do_not_change_saved_search_or_matches(self):
        created = self.client.put(self._profile_url(), json={
            "expected_version": None,
            "profile": self._definition({"skills": 60, "ai_intensity": 40}),
        })
        self.assertEqual(created.status_code, 200, created.text)
        saved = created.json()["profile"]
        self._materialize_profiled_match(saved)
        before = self._snapshot()

        invalid = self.client.put(self._profile_url(), json={
            "expected_version": saved["version"],
            "profile": self._definition({"skills": -1, "ai_intensity": 101}),
        })
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(self._snapshot(), before)

        stale = self.client.put(self._profile_url(), json={
            "expected_version": "obsolete-version",
            "profile": self._definition({"skills": 50}),
        })
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self._snapshot(), before)

        foreign_search = self.client.put(self._profile_url(search_id="other-search"), json={
            "expected_version": None,
            "profile": self._definition({"skills": 50}),
        })
        self.assertEqual(foreign_search.status_code, 409, foreign_search.text)
        self.assertEqual(self.client.get(
            self._profile_url(search_id="second-search")).json(), {"profile": None})
        foreign_explanation = self.client.get(
            self._explanation_url(search_id="other-search"))
        self.assertEqual(foreign_explanation.status_code, 404, foreign_explanation.text)
        self.assertEqual(self._snapshot(), before)

        self.assertEqual(self.client.get(self._explanation_url(profile_id="other")).status_code, 404)
        self.assertEqual(self.client.get(self._explanation_url(job_id="missing-job")).status_code, 404)
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
