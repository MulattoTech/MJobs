from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.matching_explanations import create_matching_explanations_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.search_profiles import save_search_profile
from tests.integration.test_spine import PROFILE


class MatchingExplanationsApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-matching-explanations-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'matches.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        self._save_search("selected-search", "fixture")
        self._save_search("other-search", "other")
        self._seed_job_and_match()
        app = FastAPI()
        app.include_router(create_matching_explanations_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id):
        save_search_profile(self.store, {
            "id": search_id, "profile_id": profile_id, "name": search_id,
            "enabled": False, "include_titles": [], "exclude_titles": [],
            "required_keywords": [], "optional_keywords": [], "excluded_keywords": [],
            "exact_phrases": [], "technology_aliases": {}, "remote": "ANY",
            "employment_types": [], "source_ids": [],
        })

    def _seed_job_and_match(self, *, profile_version=None, components=None, score=72.5):
        current = self.store.profile("fixture")
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Employer", domain="employer.example"))
            connection.execute(m.jobs.insert().values(
                id="job", company_id="company", requisition="req", title="Systems Engineer",
                description="Description", skills=["linux"], location="Remote", remote_type="REMOTE",
                attributes={"specialized_signals": {"ai_intensity": {
                    "score": 30, "provenance": {"source": "job_metadata", "evidence_ref": "signal-1"},
                }}}))
            connection.execute(m.jobs.insert().values(
                id="job-without-match", company_id="company", requisition="req-2",
                title="Unmatched Role", description="Description", skills=[], location="Remote",
                remote_type="REMOTE", attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id="match", job_id="job", profile_id="fixture",
                profile_version=profile_version or current["version"], score=score, missing=[],
                components=components or {
                    "scoring_profile": {"selection": "LEGACY_DEFAULT", "profile_id": None,
                                        "profile_version": None, "requested_role_family": None,
                                        "selected_role_family": None},
                    "scoring_contributions": [{
                        "dimension": "skills", "weight": 100.0, "raw_score": score,
                        "weighted_contribution": score,
                        "explanation": "Legacy deterministic score retained.",
                        "provenance": {"source": "LEGACY_MATCH_BASELINE"},
                    }],
                }))

    def _replace_match_components(self, components, *, profile_version=None):
        values = {"components": components}
        if profile_version is not None:
            values["profile_version"] = profile_version
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.update().where(
                m.matches.c.id == "match",
            ).values(**values))

    def _url(self, profile="fixture", search="selected-search", job="job"):
        return f"/profiles/{profile}/search-profiles/{search}/jobs/{job}/match-explanation"

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def _save_scoring_profile(self, *, version="version-current", weights=None):
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one()
            criteria = dict(row["criteria"])
            criteria["role_scoring_profile"] = {
                "id": "systems-profile", "version": version, "schema_version": 1,
                "role_family": "Systems", "weights": weights or {"skills": 70, "ai_intensity": 30},
                "thresholds": {},
            }
            connection.execute(m.search_profiles.update().where(
                m.search_profiles.c.id == "selected-search",
            ).values(criteria=criteria))

    def test_legacy_projection_preserves_saved_score_and_is_read_only(self):
        before = self._snapshot()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "profile_id": "fixture", "search_profile_id": "selected-search", "job_id": "job",
            "score": 72.5, "resolution": "LEGACY_DEFAULT",
            "profile_resolution": {
                "requested_role_family": None, "selected_role_family": None,
                "profile_id": None, "profile_version": None, "selection": "LEGACY_DEFAULT",
            },
            "scoring_profile": None,
            "scoring_contributions": [{
                "dimension": "skills", "weight": 100.0, "raw_score": 72.5,
                "weighted_contribution": 72.5,
                "explanation": "Legacy deterministic score retained.",
                "provenance": {"source": "LEGACY_MATCH_BASELINE"},
            }],
        })
        self.assertEqual(self._snapshot(), before)

    def test_legacy_projection_supplies_baseline_when_old_match_has_no_attribution(self):
        self._replace_match_components({})
        body = self.client.get(self._url()).json()
        self.assertEqual(body["resolution"], "LEGACY_DEFAULT")
        self.assertEqual(body["scoring_contributions"], [{
            "dimension": "skills", "weight": 100.0, "raw_score": 72.5,
            "weighted_contribution": 72.5,
            "explanation": (
                "The saved deterministic match score is preserved because no scoring profile was selected."
            ),
            "provenance": {"source": "LEGACY_MATCH_BASELINE"},
        }])

    def test_current_projection_binds_selected_search_and_keeps_unknown_explicit(self):
        self._save_scoring_profile()
        contributions = [{
            "dimension": "skills", "weight": 70.0, "raw_score": 72.5,
            "weighted_contribution": 72.5, "explanation": "Stored score contribution.",
            "provenance": {"source": "LEGACY_MATCH_BASELINE"},
        }]
        self._replace_match_components({
            "scoring_profile": {
                "selection": "ROLE_FAMILY", "requested_role_family": "Systems",
                "selected_role_family": "Systems", "profile_id": "systems-profile",
                "profile_version": "version-current",
            },
            "scoring_contributions": contributions,
        })
        before = self._snapshot()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["score"], 72.5)
        self.assertEqual(body["resolution"], "CURRENT")
        self.assertEqual(body["scoring_profile"], {
            "id": "systems-profile", "version": "version-current", "role_family": "Systems",
        })
        self.assertEqual(body["profile_resolution"]["selection"], "ROLE_FAMILY")
        self.assertEqual(body["profile_resolution"]["profile_id"], "systems-profile")
        self.assertEqual(body["profile_resolution"]["profile_version"], "version-current")
        by_dimension = {row["dimension"]: row for row in body["scoring_contributions"]}
        self.assertEqual(by_dimension["skills"], contributions[0])
        self.assertEqual(by_dimension["ai_intensity"], {
            "dimension": "ai_intensity", "weight": 30.0, "raw_score": None,
            "weighted_contribution": None,
            "explanation": "ai_intensity is UNKNOWN because the stored match has no contribution for this dimension.",
            "provenance": {"source": "UNKNOWN"},
        })
        self.assertEqual(body["score"], before[m.matches.name][0]["score"])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.client.get(self._url(profile="other")).status_code, 404)
        self.assertEqual(self.client.get(self._url(search="other-search")).status_code, 404)

    def test_stale_scoring_and_career_profile_versions_fail_without_writes(self):
        self._save_scoring_profile(version="version-new")
        self._replace_match_components({
            "scoring_profile": {"selection": "ROLE_FAMILY", "profile_id": "systems-profile",
                                 "profile_version": "version-old", "requested_role_family": "Systems",
                                 "selected_role_family": "Systems"},
            "scoring_contributions": [],
        })
        before = self._snapshot()
        stale_scoring = self.client.get(self._url())
        self.assertEqual(stale_scoring.status_code, 409, stale_scoring.text)
        self.assertEqual(stale_scoring.json()["detail"]["code"], "MATCH_EXPLANATION_STALE_CONTEXT")
        self.assertEqual(self._snapshot(), before)

        self._replace_match_components(
            {"scoring_profile": {"selection": "ROLE_FAMILY",
                "profile_id": "systems-profile", "profile_version": "version-new",
                "requested_role_family": "Systems", "selected_role_family": "Systems"},
             "scoring_contributions": []},
            profile_version="historical-profile-version",
        )
        before_stale_profile = self._snapshot()
        stale_profile = self.client.get(self._url())
        self.assertEqual(stale_profile.status_code, 409, stale_profile.text)
        self.assertEqual(stale_profile.json()["detail"]["code"], "MATCH_EXPLANATION_STALE_CONTEXT")
        self.assertEqual(self._snapshot(), before_stale_profile)

    def test_missing_objects_return_404(self):
        self.assertEqual(self.client.get(self._url(job="missing-job")).status_code, 404)
        self.assertEqual(self.client.get(self._url(search="missing-search")).status_code, 404)
        self.assertEqual(self.client.get(self._url(profile="missing-profile")).status_code, 404)
        self.assertEqual(self.client.get(self._url(job="job-without-match")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
