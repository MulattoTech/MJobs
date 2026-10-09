"""Contribution integrity for persisted role-scoring match explanations."""

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
from services.matching.profile_scoring import resolve_and_compose_profile_scoring
from tests.integration.test_spine import PROFILE


class MatchingExplanationContributionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-match-contributions-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'contributions.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({
            **deepcopy(PROFILE), "id": "other", "email": "other@example.test",
        })
        self._save_search("selected-search", "fixture")
        self._save_search("other-search", "other")
        self._seed_jobs()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            operation_mode="PERSONAL",
        ))
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id):
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
            "source_ids": [],
        })

    def _seed_jobs(self):
        other_version = self.store.profile("other")["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="contribution-company", name="Contribution Fixture Co.",
                domain="contribution.example",
            ))
            connection.execute(m.jobs.insert().values(
                id="selected-job", company_id="contribution-company",
                requisition="selected-req", title="Cloud Administrator",
                description="Posting text alone cannot establish hourly-compensation suitability.",
                skills=["Linux"], location="Remote", remote_type="REMOTE",
                attributes={"specialized_signals": {
                    "ai_intensity": {
                        "score": 50,
                        "provenance": {
                            "source": "EMPLOYER_POSTING_METADATA",
                            "evidence_ref": "posting:ai-intensity-7",
                        },
                    },
                }},
            ))
            connection.execute(m.jobs.insert().values(
                id="other-profile-job", company_id="contribution-company",
                requisition="other-profile-req", title="Other profile's role",
                description="Another profile's saved match.", skills=[], location="Remote",
                remote_type="REMOTE", attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id="other-profile-match", job_id="other-profile-job", profile_id="other",
                profile_version=other_version, score=44.0, missing=[], components={},
            ))

    @staticmethod
    def _profile_url():
        return "/profiles/fixture/search-profiles/selected-search/role-scoring-profile"

    @staticmethod
    def _explanation_url(profile_id="fixture", search_id="selected-search", job_id="selected-job"):
        return (f"/profiles/{profile_id}/search-profiles/{search_id}/jobs/{job_id}"
                "/match-explanation")

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(
                    select(table),
                ).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_contributions_recompose_score_preserve_unknown_provenance_and_scope(self):
        configured = self.client.put(self._profile_url(), json={
            "expected_version": None,
            "profile": {
                "schema_version": 1,
                "role_family": "Cloud Operations",
                "weights": {
                    "skills": 80,
                    "ai_intensity": 20,
                    "hourly_comp_suitability": 30,
                },
                "thresholds": {},
            },
        })
        self.assertEqual(configured.status_code, 200, configured.text)
        saved_profile = configured.json()["profile"]

        with self.store.engine.connect() as connection:
            job = dict(connection.execute(select(m.jobs).where(
                m.jobs.c.id == "selected-job",
            )).mappings().one())
        career_profile = self.store.profile("fixture")
        legacy_match = {"score": 80.0, "missing": [], "components": {}}
        expected = resolve_and_compose_profile_scoring(
            job,
            legacy_match,
            role_family="cloud operations",
            profiles_by_role_family={saved_profile["role_family"]: saved_profile},
        )
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.insert().values(
                id="selected-match", job_id="selected-job", profile_id="fixture",
                profile_version=career_profile["version"], score=expected["score"],
                missing=[], components={
                    "scoring_profile": expected["scoring_profile"],
                    "scoring_contributions": expected["scoring_contributions"],
                },
            ))

        before_reads = self._snapshot()
        response = self.client.get(self._explanation_url())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["score"], 74.0)
        self.assertEqual(body["scoring_profile"]["id"], saved_profile["id"])
        self.assertEqual(body["scoring_profile"]["version"], saved_profile["version"])
        self.assertEqual(body["scoring_contributions"], expected["scoring_contributions"])

        rows = {row["dimension"]: row for row in body["scoring_contributions"]}
        self.assertEqual(rows["ai_intensity"]["provenance"], {
            "source": "EMPLOYER_POSTING_METADATA",
            "evidence_ref": "posting:ai-intensity-7",
        })
        self.assertIsNone(rows["hourly_comp_suitability"]["raw_score"])
        self.assertIsNone(rows["hourly_comp_suitability"]["weighted_contribution"])
        self.assertEqual(rows["hourly_comp_suitability"]["provenance"], {"source": "UNKNOWN"})
        known_contributions = [
            row["weighted_contribution"]
            for row in body["scoring_contributions"]
            if row["raw_score"] is not None
        ]
        self.assertEqual(sum(known_contributions), body["score"])
        self.assertEqual(sum(known_contributions), 74.0)

        wrong_profile = self.client.get(self._explanation_url(profile_id="other"))
        self.assertEqual(wrong_profile.status_code, 404)
        self.assertEqual(wrong_profile.json()["detail"]["code"],
                         "MATCH_EXPLANATION_SEARCH_NOT_FOUND")
        wrong_search = self.client.get(self._explanation_url(search_id="other-search"))
        self.assertEqual(wrong_search.status_code, 404)
        self.assertEqual(wrong_search.json()["detail"]["code"],
                         "MATCH_EXPLANATION_SEARCH_NOT_FOUND")
        foreign_job = self.client.get(self._explanation_url(job_id="other-profile-job"))
        self.assertEqual(foreign_job.status_code, 404)
        self.assertEqual(foreign_job.json()["detail"]["code"],
                         "MATCH_EXPLANATION_MATCH_NOT_FOUND")
        self.assertEqual(self._snapshot(), before_reads)

    def test_inconsistent_stored_contributions_fail_closed_without_writes(self):
        configured = self.client.put(self._profile_url(), json={
            "expected_version": None,
            "profile": {
                "schema_version": 1,
                "role_family": "Cloud Operations",
                "weights": {"skills": 100},
                "thresholds": {},
            },
        })
        self.assertEqual(configured.status_code, 200, configured.text)
        saved_profile = configured.json()["profile"]
        career_profile = self.store.profile("fixture")
        components = {
            "scoring_profile": {
                "selection": "ROLE_FAMILY",
                "requested_role_family": "cloud operations",
                "selected_role_family": "Cloud Operations",
                "profile_id": saved_profile["id"],
                "profile_version": saved_profile["version"],
            },
            "scoring_contributions": [{
                "dimension": "skills",
                "weight": 100.0,
                "raw_score": 80.0,
                "weighted_contribution": 69.0,
                "explanation": "Intentionally inconsistent persisted contribution.",
                "provenance": {"source": "LEGACY_MATCH_BASELINE"},
            }],
        }
        with self.store.engine.begin() as connection:
            connection.execute(m.matches.insert().values(
                id="inconsistent-match", job_id="selected-job", profile_id="fixture",
                profile_version=career_profile["version"], score=80.0, missing=[],
                components=components,
            ))

        before_read = self._snapshot()
        response = self.client.get(self._explanation_url())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "MATCH_EXPLANATION_STALE_CONTEXT")
        self.assertEqual(self._snapshot(), before_read)

    def test_recalculation_persists_experience_weight_from_verified_profile_facts(self):
        self.store.save_profile({
            **deepcopy(PROFILE),
            "facts": [
                {"id": "verified-skill", "kind": "skill", "text": "Linux",
                 "source": "synthetic profile", "confidence": 100,
                 "verified": True, "user_approved": True},
                {"id": "approved-experience", "kind": "experience",
                 "text": "Administered Active Directory", "source": "synthetic profile",
                 "confidence": 100, "verified": True, "user_approved": True},
            ],
        })
        configured = self.client.put(self._profile_url(), json={
            "expected_version": None,
            "profile": {
                "schema_version": 1,
                "role_family": "Systems Administration",
                "weights": {"skills": 0, "experience": 100},
                "thresholds": {},
            },
        })
        self.assertEqual(configured.status_code, 200, configured.text)
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(
                m.jobs.c.id == "selected-job",
            ).values(skills=["Linux", "Active Directory", "Windows Server"]))
            search = dict(connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one())

        from services.discovery.matching import recalculate
        recalculate(self.store, "fixture", scoring_criteria=search["criteria"])

        with self.store.engine.connect() as connection:
            saved = dict(connection.execute(select(m.matches).where(
                m.matches.c.job_id == "selected-job",
                m.matches.c.profile_id == "fixture",
                m.matches.c.profile_version == self.store.profile("fixture")["version"],
            )).mappings().one())
        self.assertEqual(saved["score"], 33.33)
        explanation = self.client.get(self._explanation_url())
        self.assertEqual(explanation.status_code, 200, explanation.text)
        experience = next(row for row in explanation.json()["scoring_contributions"]
                          if row["dimension"] == "experience")
        self.assertEqual(experience["raw_score"], 33.33)
        self.assertEqual(experience["weighted_contribution"], 33.33)
        self.assertEqual(experience["provenance"]["source"],
                         "USER_APPROVED_EXPERIENCE_FACTS")


if __name__ == "__main__":
    unittest.main()
