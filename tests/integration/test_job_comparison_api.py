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

    def test_current_scored_job_excluded_by_saved_search_fails_without_writes(self):
        for criteria in (
            {"include_titles": ["Systems Administrator"]},
            {"exclude_titles": ["Infrastructure"]},
            {"required_keywords": ["missing synthetic keyword"]},
        ):
            with self.subTest(criteria=criteria):
                with self.store.engine.begin() as connection:
                    connection.execute(m.search_profiles.update().where(
                        m.search_profiles.c.id == "selected-search",
                    ).values(criteria=criteria))
                before = self._snapshot()
                response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"], "JOB_COMPARISON_SCOPE_MISMATCH")
                self.assertEqual(self.service_calls, [])
                self.assertEqual(self._snapshot(), before)

    def test_saved_jobs_filters_bound_comparison_without_writes(self):
        with self.store.engine.begin() as connection:
            # Higher-ranked, unselected rows must not consume a comparison page.
            for ordinal in range(3):
                job_id = f"unselected-{ordinal}"
                connection.execute(m.jobs.insert().values(
                    id=job_id, company_id="company", requisition=job_id,
                    title="Unselected Engineer", description="Synthetic fixture",
                    skills=[], location="Unknown", remote_type="UNKNOWN", attributes={},
                ))
                connection.execute(m.routes.insert().values(
                    id=f"route-{job_id}", job_id=job_id, company_id="company",
                    provider="fixture", tenant="comparison-fixture", requisition=job_id,
                    url=f"https://jobs.example.test/{job_id}",
                ))
                connection.execute(m.matches.insert().values(
                    id=f"match-{job_id}", job_id=job_id, profile_id="fixture",
                    profile_version=self.profile["version"], score=99.0, missing=[], components={},
                ))
            for job_id in ("job-one", "job-two"):
                connection.execute(m.routes.insert().values(
                    id=f"route-{job_id}", job_id=job_id, company_id="company",
                    provider="fixture", tenant="comparison-fixture", requisition=job_id,
                    url=f"https://jobs.example.test/{job_id}",
                ))
        for minimum, expected in (("72", 409), ("70", 200)):
            with self.subTest(minimum=minimum):
                with self.store.engine.begin() as connection:
                    connection.execute(m.search_profiles.update().where(
                        m.search_profiles.c.id == "selected-search",
                    ).values(criteria={"job_filters": {
                        "q": "", "remote_type": "", "provider": "", "sort": "score",
                        "min_match_score": minimum,
                    }}))
                before = self._snapshot()
                response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
                self.assertEqual(response.status_code, expected, response.text)
                if expected == 409:
                    self.assertEqual(response.json()["detail"]["code"], "JOB_COMPARISON_SCOPE_MISMATCH")
                    self.assertEqual(self.service_calls, [])
                self.assertEqual(self._snapshot(), before)

    def _prepare_salary_comparison(self, first_salary, second_salary, filters):
        with self.store.engine.begin() as connection:
            for job_id, salary in (("job-one", first_salary), ("job-two", second_salary)):
                original = connection.execute(select(m.jobs.c.attributes).where(
                    m.jobs.c.id == job_id,
                )).scalar_one()
                attributes = {key: value for key, value in original.items()
                              if not key.startswith("salary_")}
                attributes.update(salary)
                connection.execute(m.jobs.update().where(m.jobs.c.id == job_id).values(
                    attributes=attributes,
                ))
                route_id = f"route-{job_id}"
                if connection.execute(select(m.routes.c.id).where(
                    m.routes.c.id == route_id,
                )).scalar_one_or_none() is None:
                    connection.execute(m.routes.insert().values(
                        id=route_id, job_id=job_id, company_id="company",
                        provider="fixture", tenant="comparison-fixture", requisition=job_id,
                        url=f"https://jobs.example.test/{job_id}",
                    ))
            connection.execute(m.search_profiles.update().where(
                m.search_profiles.c.id == "selected-search",
            ).values(criteria={"job_filters": {
                "q": "", "remote_type": "", "provider": "", "sort": "score", **filters,
            }}))

    def test_saved_hourly_salary_bounds_allow_matching_jobs_without_writes(self):
        salary = {"salary_min": 25, "salary_max": 45, "salary_currency": "USD",
                  "salary_period": "HOUR", "salary_source": "EMPLOYER_PROVIDED"}
        self._prepare_salary_comparison(salary, salary, {
            "salary_min": "20", "salary_max": "50", "salary_period": "HOUR",
        })
        before = self._snapshot()
        response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job_ids"], ["job-one", "job-two"])
        self.assertEqual(len(self.service_calls), 1)
        self.assertEqual(self._snapshot(), before)

    def test_saved_known_salary_policy_rejects_missing_pay_without_writes(self):
        salary = {"salary_min": 80000, "salary_max": 100000, "salary_currency": "USD",
                  "salary_period": "YEAR", "salary_source": "EMPLOYER_PROVIDED"}
        self._prepare_salary_comparison(salary, {}, {"salary_policy": "KNOWN_ONLY"})
        before = self._snapshot()
        response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "JOB_COMPARISON_SCOPE_MISMATCH")
        self.assertEqual(self.service_calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_saved_unknown_salary_policy_allows_missing_pay_without_writes(self):
        salary = {"salary_min": 80000, "salary_max": 100000, "salary_currency": "USD",
                  "salary_period": "YEAR", "salary_source": "EMPLOYER_PROVIDED"}
        self._prepare_salary_comparison(salary, {}, {
            "salary_min": "70000", "salary_max": "110000", "salary_policy": "ALLOW_UNKNOWN",
        })
        before = self._snapshot()
        response = self.client.post(self._url(), json={"job_ids": ["job-one", "job-two"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job_ids"], ["job-one", "job-two"])
        self.assertEqual(len(self.service_calls), 1)
        self.assertEqual(self._snapshot(), before)

    def test_saved_salary_policies_preserve_legacy_defaults_and_exclusions(self):
        salary = {"salary_min": 80000, "salary_max": 100000, "salary_currency": "USD",
                  "salary_period": "YEAR", "salary_source": "EMPLOYER_PROVIDED"}
        bounds = {"salary_min": "70000", "salary_max": "110000"}
        allow_unknown = {**bounds, "salary_policy": "ALLOW_UNKNOWN"}
        cases = (
            ("legacy no salary filter", {}, {}, 200),
            ("legacy annual bounds", salary, bounds, 200),
            ("legacy bounded missing pay", {}, bounds, 409),
            ("other currency", {**salary, "salary_currency": "EUR"}, allow_unknown, 409),
            ("other period", {**salary, "salary_period": "HOUR"}, allow_unknown, 409),
            ("estimated pay", {**salary, "salary_source": "ESTIMATED"}, allow_unknown, 409),
            ("outside bounds", {**salary, "salary_min": 50000, "salary_max": 60000},
             allow_unknown, 409),
        )
        for label, second_salary, filters, expected in cases:
            with self.subTest(case=label):
                self._prepare_salary_comparison(salary, second_salary, filters)
                self.service_calls.clear()
                before = self._snapshot()
                response = self.client.post(
                    self._url(), json={"job_ids": ["job-one", "job-two"]},
                )
                self.assertEqual(response.status_code, expected, response.text)
                if expected == 409:
                    self.assertEqual(response.json()["detail"]["code"], "JOB_COMPARISON_SCOPE_MISMATCH")
                    self.assertEqual(self.service_calls, [])
                self.assertEqual(self._snapshot(), before)

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
