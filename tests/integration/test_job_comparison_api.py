"""Read-only, saved-match scoped API contract for multi-job comparison."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.job_comparison import create_job_comparison_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.matching.job_comparison import compare_scored_jobs
from tests.integration.test_spine import PROFILE


class JobComparisonApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-job-comparison-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'comparison.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        self.profile = self.store.profile("fixture")
        self._save_search("selected-search", "fixture")
        self._save_search("other-search", "fixture")
        self._save_search("foreign-search", "other")
        self._seed_jobs_and_matches()

        self.service_calls = []
        app = FastAPI()
        app.include_router(create_job_comparison_router(
            self.store, compare_scored_jobs=self._compare_scored_jobs,
        ))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _save_search(self, search_id, profile_id):
        with self.store.engine.begin() as connection:
            connection.execute(m.search_profiles.insert().values(
                id=search_id, profile_id=profile_id, name=search_id, enabled=False,
                criteria={}, source_ids=[], created_at="fixture", updated_at="fixture",
            ))

    def _seed_jobs_and_matches(self):
        other_profile_version = self.store.profile("other")["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Example Employer", domain="example.test",
            ))
            for job_id, title, required in (
                ("job-one", "Systems Administrator", ["ACTIVE_DIRECTORY", "POWERSHELL"]),
                ("job-two", "Infrastructure Engineer", ["ACTIVE_DIRECTORY", "LINUX"]),
                ("job-unmatched", "Unmatched Engineer", ["LINUX"]),
                ("job-foreign", "Foreign Engineer", ["LINUX"]),
                ("job-stale", "Stale Engineer", ["LINUX"]),
            ):
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="company", requisition=job_id, title=title,
                    description="Synthetic fixture only.", skills=[], location="Unknown",
                    remote_type="UNKNOWN",
                    attributes={
                        "skill_evidence": "structured_requirements",
                        "required_skills": required,
                        "source_type": "fixture_source",
                        "requirement_source_ref": f"fixture:{job_id}:requirements",
                    },
                ))
            for ordinal, job_id in enumerate(("job-one", "job-two", "job-stale"), start=1):
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id, profile_id="fixture",
                    profile_version=("old-profile-version" if job_id == "job-stale"
                                     else self.profile["version"]),
                    score=70.0 + ordinal, missing=[],
                    components={"scoring_contributions": [{
                        "dimension": "skills", "weight": 100.0, "raw_score": 70.0 + ordinal,
                        "weighted_contribution": 70.0 + ordinal,
                        "explanation": "Saved synthetic match score.",
                        "provenance": {"source": "fixture_match"},
                    }]},
                ))
            connection.execute(m.matches.insert().values(
                id="match-job-foreign", job_id="job-foreign", profile_id="other",
                profile_version=other_profile_version, score=80.0,
                missing=[], components={"scoring_contributions": []},
            ))

    def _compare_scored_jobs(self, *, profile_id, search_profile_id, job_ids, snapshots):
        self.service_calls.append({
            "profile_id": profile_id,
            "search_profile_id": search_profile_id,
            "job_ids": list(job_ids),
            "snapshots": deepcopy(snapshots),
        })
        return compare_scored_jobs(
            profile_id=profile_id,
            search_profile_id=search_profile_id,
            job_ids=job_ids,
            snapshots=snapshots,
        )

    def _url(self, profile="fixture", search="selected-search"):
        return f"/profiles/{profile}/search-profiles/{search}/jobs/compare"

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def test_projects_only_current_scoped_saved_matches_and_returns_service_contract(self):
        before = self._snapshot()
        response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["search_profile_id"], "selected-search")
        self.assertEqual([row["job_id"] for row in body["jobs"]], ["job-one", "job-two"])
        self.assertEqual(body["shared_requirements"][0]["term"], "active directory")
        self.assertEqual(body["unknowns"], [])
        self.assertEqual(len(self.service_calls), 1)
        call = self.service_calls[0]
        self.assertEqual(call["profile_id"], "fixture")
        self.assertEqual(call["search_profile_id"], "selected-search")
        self.assertEqual(call["job_ids"], ["job-one", "job-two"])
        self.assertEqual(call["snapshots"], [
            {
                "job_id": "job-one", "title": "Systems Administrator",
                "company": "Example Employer", "match_score": 71.0,
                "scoring_contributions": [{
                    "dimension": "skills", "weight": 100.0, "raw_score": 71.0,
                    "weighted_contribution": 71.0,
                    "explanation": "Saved synthetic match score.",
                    "provenance": {"source": "fixture_match"},
                }],
                "required_requirements": [
                    {"normalized_term": "active directory", "provenance": {
                        "source": "fixture_source", "evidence_ref": "fixture:job-one:requirements"}},
                    {"normalized_term": "powershell", "provenance": {
                        "source": "fixture_source", "evidence_ref": "fixture:job-one:requirements"}},
                ],
            },
            {
                "job_id": "job-two", "title": "Infrastructure Engineer",
                "company": "Example Employer", "match_score": 72.0,
                "scoring_contributions": [{
                    "dimension": "skills", "weight": 100.0, "raw_score": 72.0,
                    "weighted_contribution": 72.0,
                    "explanation": "Saved synthetic match score.",
                    "provenance": {"source": "fixture_match"},
                }],
                "required_requirements": [
                    {"normalized_term": "active directory", "provenance": {
                        "source": "fixture_source", "evidence_ref": "fixture:job-two:requirements"}},
                    {"normalized_term": "linux", "provenance": {
                        "source": "fixture_source", "evidence_ref": "fixture:job-two:requirements"}},
                ],
            },
        ])
        self.assertEqual(self._snapshot(), before)

    def test_unstructured_mentions_are_not_promoted_to_comparison_requirements(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "job-two").values(
                attributes={"skill_evidence": "description_mentions",
                            "required_skills": ["LINUX"], "source_type": "fixture_source"},
            ))
        response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
        self.assertEqual(response.status_code, 200, response.text)
        second = self.service_calls[-1]["snapshots"][1]
        self.assertNotIn("required_requirements", second)
        self.assertEqual(response.json()["unknowns"], [{
            "category": "required_terms", "job_ids": ["job-two"],
            "reason": "REQUIREMENTS_MISSING",
        }])

    def test_structured_terms_without_source_provenance_remain_unknown(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "job-two").values(
                attributes={"skill_evidence": "structured_required_skills",
                            "required_skills": ["LINUX"]},
            ))
        response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
        self.assertEqual(response.status_code, 200, response.text)
        second = self.service_calls[-1]["snapshots"][1]
        self.assertNotIn("required_requirements", second)
        self.assertEqual(response.json()["unknowns"], [{
            "category": "required_terms", "job_ids": ["job-two"],
            "reason": "REQUIREMENTS_MISSING",
        }])

    def test_foreign_search_unmatched_foreign_and_stale_jobs_fail_without_writes(self):
        before = self._snapshot()
        foreign_search = self.client.post(
            self._url(profile="fixture", search="foreign-search"),
            json={"job_ids": ["job-one", "job-two"]},
        )
        self.assertEqual(foreign_search.status_code, 409, foreign_search.text)
        self.assertEqual(foreign_search.json()["detail"]["code"], "JOB_COMPARISON_SCOPE_MISMATCH")
        for job_id, code in (
            ("job-unmatched", "JOB_COMPARISON_SCOPE_MISMATCH"),
            ("job-foreign", "JOB_COMPARISON_SCOPE_MISMATCH"),
            ("job-stale", "JOB_COMPARISON_STALE_CONTEXT"),
        ):
            response = self.client.post(self._url(), json={"job_ids": ["job-one", job_id]})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], code)
        self.assertEqual(self.service_calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_job_count_and_uniqueness_bounds_fail_before_read_service(self):
        for ids in ([], ["job-one"], ["job-one", "job-one"],
                    [f"job-{index}" for index in range(11)], ["x" * 129, "job-two"]):
            with self.subTest(job_ids=ids):
                response = self.client.post(self._url(), json={"job_ids": ids})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.service_calls, [])

    def test_scoring_configuration_drift_is_stale_and_read_only(self):
        with self.store.engine.begin() as connection:
            search = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one()
            criteria = dict(search["criteria"])
            criteria["role_scoring_profile"] = {
                "id": "systems-profile", "version": "new-version", "schema_version": 1,
                "role_family": "Systems", "weights": {"skills": 100}, "thresholds": {},
            }
            connection.execute(m.search_profiles.update().where(
                m.search_profiles.c.id == "selected-search",
            ).values(criteria=criteria))
        before = self._snapshot()
        response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "JOB_COMPARISON_STALE_CONTEXT")
        self.assertEqual(self.service_calls, [])
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
