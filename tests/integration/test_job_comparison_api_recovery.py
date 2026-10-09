"""Failure and recovery boundaries for the read-only job comparison API."""

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


class JobComparisonApiRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-job-comparison-recovery-")
        self.addCleanup(self.directory.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.directory.name) / 'comparison.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        self.profile = self.store.profile("fixture")
        self._seed_searches()
        self._seed_jobs_and_matches()

        self.service_calls = []
        app = FastAPI()
        app.include_router(create_job_comparison_router(
            self.store, compare_scored_jobs=self._compare_scored_jobs,
        ))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_searches(self):
        with self.store.engine.begin() as connection:
            for search_id, profile_id in (
                ("selected-search", "fixture"),
                ("foreign-search", "other"),
            ):
                connection.execute(m.search_profiles.insert().values(
                    id=search_id, profile_id=profile_id, name=search_id, enabled=False,
                    criteria={}, source_ids=[], created_at="fixture", updated_at="fixture",
                ))

    def _seed_jobs_and_matches(self):
        other_profile_version = self.store.profile("other")["version"]
        additional_current = [f"selected-extra-{index}" for index in range(1, 9)]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Synthetic Employer", domain="synthetic.example",
            ))
            for job_id in (
                "current-one", "current-two", *additional_current,
                "unmatched", "foreign", "stale",
            ):
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="company", requisition=job_id,
                    title=f"Synthetic {job_id}", description="Local test fixture.",
                    skills=[], location="Unknown", remote_type="UNKNOWN",
                    attributes={
                        "skill_evidence": "structured_required_skills",
                        "required_skills": ["LINUX"], "source_type": "synthetic_fixture",
                        "source_ref": f"fixture:{job_id}",
                    },
                ))
            current_matches = [
                ("current-one", 68.0), ("current-two", 74.0),
                *[(job_id, 60.0 + index)
                  for index, job_id in enumerate(additional_current, start=1)],
            ]
            matches_to_insert = [
                *((job_id, score, "fixture", self.profile["version"])
                  for job_id, score in current_matches),
                ("stale", 82.0, "fixture", "previous-profile-version"),
                ("foreign", 90.0, "other", other_profile_version),
            ]
            for job_id, score, match_profile_id, version in matches_to_insert:
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id,
                    profile_id=match_profile_id,
                    profile_version=version, score=score, missing=[],
                    components={"scoring_contributions": [{
                        "dimension": "skills", "weight": 100.0, "raw_score": score,
                        "weighted_contribution": score,
                        "explanation": "Saved synthetic score.",
                        "provenance": {"source": "synthetic_fixture"},
                    }]},
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

    def _assert_safe_failure_without_mutation(self, response, *, status, code):
        self.assertEqual(response.status_code, status, response.text)
        detail = response.json()["detail"]
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail.get("code"), code)
        self.assertNotIn("SQL", str(detail).upper())

    def test_missing_foreign_and_stale_snapshots_fail_closed_without_mutation(self):
        before = self._snapshot()
        cases = (
            ("current-one", "unmatched", "JOB_COMPARISON_SCOPE_MISMATCH"),
            ("current-one", "foreign", "JOB_COMPARISON_SCOPE_MISMATCH"),
            ("current-one", "missing-job", "JOB_COMPARISON_SCOPE_MISMATCH"),
            ("current-one", "stale", "JOB_COMPARISON_STALE_CONTEXT"),
        )
        for first, second, code in cases:
            with self.subTest(second=second):
                response = self.client.post(
                    self._url(), json={"job_ids": [first, second]},
                )
                self._assert_safe_failure_without_mutation(response, status=409, code=code)
                self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.service_calls, [])

    def test_foreign_saved_search_fails_closed_without_revealing_job_data(self):
        before = self._snapshot()
        response = self.client.post(
            self._url(profile="fixture", search="foreign-search"),
            json={"job_ids": ["current-one", "current-two"]},
        )
        self._assert_safe_failure_without_mutation(
            response, status=409, code="JOB_COMPARISON_SCOPE_MISMATCH",
        )
        self.assertEqual(self.service_calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_duplicate_and_out_of_bounds_selection_is_rejected_before_service(self):
        before = self._snapshot()
        invalid_selections = (
            [], ["current-one"], ["current-one", "current-one"],
            [f"missing-{index}" for index in range(11)],
            ["x" * 129, "current-two"],
        )
        for job_ids in invalid_selections:
            with self.subTest(count=len(job_ids), ids=job_ids[:2]):
                response = self.client.post(self._url(), json={"job_ids": job_ids})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("SQL", response.text.upper())
                self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.service_calls, [])

    def test_ten_selected_jobs_are_compared_at_the_upper_valid_bound(self):
        job_ids = ["current-one", "current-two"] + [
            f"selected-extra-{index}" for index in range(1, 9)
        ]
        before = self._snapshot()
        response = self.client.post(self._url(), json={"job_ids": job_ids})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job_ids"], job_ids)
        self.assertEqual(len(response.json()["jobs"]), 10)
        self.assertEqual(self._snapshot(), before)

    def test_stale_scoring_context_can_recover_and_compare_without_writes(self):
        with self.store.engine.begin() as connection:
            search = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == "selected-search",
            )).mappings().one()
            criteria = dict(search["criteria"])
            criteria["role_scoring_profile"] = {
                "id": "systems-profile", "version": "changed-version", "schema_version": 1,
                "role_family": "Systems", "weights": {"skills": 100}, "thresholds": {},
            }
            connection.execute(m.search_profiles.update().where(
                m.search_profiles.c.id == "selected-search",
            ).values(criteria=criteria))

        before_failure = self._snapshot()
        failed = self.client.post(
            self._url(), json={"job_ids": ["current-one", "current-two"]},
        )
        self._assert_safe_failure_without_mutation(
            failed, status=409, code="JOB_COMPARISON_STALE_CONTEXT",
        )
        self.assertEqual(self._snapshot(), before_failure)
        self.assertEqual(self.service_calls, [])

        with self.store.engine.begin() as connection:
            connection.execute(m.search_profiles.update().where(
                m.search_profiles.c.id == "selected-search",
            ).values(criteria={}))
        before_recovery = self._snapshot()
        recovered = self.client.post(
            self._url(), json={"job_ids": ["current-one", "current-two"]},
        )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        body = recovered.json()
        self.assertEqual(body["job_ids"], ["current-one", "current-two"])
        self.assertEqual([row["match_score"] for row in body["jobs"]], [68.0, 74.0])
        self.assertEqual(len(self.service_calls), 1)
        self.assertEqual(self._snapshot(), before_recovery)


if __name__ == "__main__":
    unittest.main()
