from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.application_priority import create_application_priority_router
from apps.api.company_preferences import create_company_preferences_router
from apps.api.match_feedback import create_match_feedback_router
from packages.persistence import models as m
from packages.persistence.store import Store


PROFILE = {
    "id": "fixture", "name": "Synthetic User", "email": "user@example.test",
    "answers": [], "facts": [],
}
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class ApplicationPriorityApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-application-priority-api-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'priority.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**deepcopy(PROFILE), "id": "other", "email": "other@example.test"})
        self._seed_job()
        app = FastAPI()
        app.include_router(create_application_priority_router(
            self.store, now_provider=lambda: NOW,
        ))
        app.include_router(create_match_feedback_router(self.store))
        app.include_router(create_company_preferences_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _seed_job(self, *, job_id="job", profile_id="fixture", attributes=None,
                  components=None, score=80.0, profile_version=None, postings=()):
        profile = self.store.profile(profile_id)
        company_id = f"company-{job_id}"
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=company_id, name="Synthetic Employer", domain="employer.example",
            ))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=company_id, requisition=f"req-{job_id}",
                title="Systems Engineer", description="Synthetic description",
                skills=["linux"], location="Remote", remote_type="REMOTE",
                attributes=attributes or {},
            ))
            connection.execute(m.matches.insert().values(
                id=f"match-{profile_id}-{job_id}", job_id=job_id,
                profile_id=profile_id, profile_version=profile_version or profile["version"],
                score=score, missing=[], components=components or {},
            ))
            for index, fetched_at in enumerate(postings):
                connection.execute(m.postings.insert().values(
                    id=f"posting-{job_id}-{index}", job_id=job_id,
                    source="synthetic_source", source_job_id=f"source-{job_id}-{index}",
                    source_url=f"https://source.example.test/jobs/{index}",
                    application_url=f"https://apply.example.test/jobs/{index}",
                    raw={}, fetched_at=fetched_at,
                ))
        return {"id": job_id, "company_id": company_id}

    def _set_job_signals(self, *, attributes, components, postings=()):
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.update().where(m.jobs.c.id == "job").values(
                attributes=attributes,
            ))
            connection.execute(m.matches.update().where(
                m.matches.c.job_id == "job", m.matches.c.profile_id == "fixture",
            ).values(components=components))
            for index, fetched_at in enumerate(postings):
                connection.execute(m.postings.insert().values(
                    id=f"posting-job-{index}", job_id="job",
                    source="synthetic_source", source_job_id=f"source-job-{index}",
                    source_url=f"https://source.example.test/jobs/{index}",
                    application_url=f"https://apply.example.test/jobs/{index}",
                    raw={}, fetched_at=fetched_at,
                ))

    @staticmethod
    def _evidence(classification="STAFFING_AGENCY", *, subject_id="company-job"):
        return {
            "classification": classification, "subject_type": "COMPANY",
            "subject_id": subject_id, "source": "synthetic_registry",
            "evidence_ref": "fixture://company-classification/1", "current": True,
            "explicit": True, "confidence": 0.86,
        }

    @staticmethod
    def _components(salary_status="ALIGNS"):
        return {"fit_factors": [{
            "id": "salary", "label": "Salary expectation", "status": salary_status,
            "explanation": "Stored salary comparison.", "score_effect": "NONE",
        }]}

    def _url(self, profile_id="fixture", job_id="job"):
        return f"/profiles/{profile_id}/jobs/{job_id}/application-priority"

    def _snapshot(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: [dict(row) for row in connection.execute(select(table)).mappings()]
                for table in m.metadata.sorted_tables
            }

    def _set_coverage_signals(self, *, work_arrangement=False, expiration=True):
        attributes = {
            "reported_posted_at": "2026-10-08T12:00:00Z",
            "application_complexity": "MEDIUM",
            "experience_level": "SENIOR",
            "recruiter_classification_evidence": [self._evidence()],
        }
        if expiration:
            attributes["expires_at"] = "2026-10-12T12:00:00Z"
        components = self._components()
        if work_arrangement:
            components["fit_factors"].append({
                "id": "work_arrangement", "status": "ALIGNS",
            })
        self._set_job_signals(
            attributes=attributes, components=components,
            postings=("2026-10-08T12:00:00Z",),
        )
        saved = self.client.put(
            "/profiles/fixture/company-preferences/company-job",
            json={"status": "NEUTRAL", "request_id": "coverage-company"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)

    def _apply_coverage_feedback(self, category):
        feedback = self.client.post("/profiles/fixture/jobs/job/match-feedback", json={
            "category": category, "request_id": f"coverage-feedback-{category}",
        })
        self.assertEqual(feedback.status_code, 200, feedback.text)
        proposal = feedback.json()["preference_proposal"]
        applied = self.client.post("/profiles/fixture/match-preferences", json={
            "feedback_id": feedback.json()["feedback"]["id"],
            "dimension": proposal["dimension"], "direction": proposal["direction"],
            "request_id": f"coverage-apply-{category}",
        })
        self.assertEqual(applied.status_code, 200, applied.text)

    def _read_priority_without_writes(self, *, client=None):
        before = self._snapshot()
        response = (client or self.client).get(self._url())
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["match_score"], 80.0)
        self.assertEqual(before[m.matches.name][0]["score"], 80.0)
        self.assertTrue(body["advisory"])
        self.assertFalse(body["affects_match_score"])
        self.assertEqual(before[m.applications.name], [])
        self.assertEqual(before[m.operations.name], [])
        return body

    def test_complete_persisted_evidence_with_work_arrangement_has_full_coverage(self):
        self._set_coverage_signals(work_arrangement=True)

        body = self._read_priority_without_writes()

        self.assertEqual(body["coverage_percent"], 100)
        self.assertEqual(body["unknowns"], [])
        self.assertEqual(body["priority_score"], 85.0)
        factors = {row["key"]: row for row in body["factors"]}
        self.assertEqual(factors["work_arrangement"]["status"], "KNOWN")
        self.assertEqual(factors["work_arrangement"]["contribution"], 10.0)
        self.assertEqual(factors["company_preference"]["status"], "KNOWN")
        self.assertTrue(all(row["status"] == "KNOWN" for row in body["factors"]))

    def test_reviewed_weights_preserve_full_coverage_after_database_reopen(self):
        self._set_coverage_signals()
        preferences_before = self.store.profile("fixture")["preferences"]
        initial = self._read_priority_without_writes()
        self.assertEqual(initial["coverage_percent"], 100)
        final_body = None
        for category, factor_key, contribution in (
            ("BAD_SALARY", "salary", 20.0),
            ("TOO_SENIOR", "seniority", 3.0),
        ):
            self._apply_coverage_feedback(category)
            with self.subTest(category=category):
                body = self._read_priority_without_writes()
                self.assertEqual(body["coverage_percent"], 100)
                self.assertEqual(body["unknowns"], [])
                factor = next(row for row in body["factors"] if row["key"] == factor_key)
                self.assertEqual(factor["contribution"], contribution)
                self.assertIn("user-reviewed", factor["explanation"].lower())
                self.assertNotEqual(body["priority_score"], initial["priority_score"])
                final_body = body
        self.assertEqual(self.store.profile("fixture")["preferences"], preferences_before)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(self.root / 'priority.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        app = FastAPI()
        app.include_router(create_application_priority_router(
            self.store, now_provider=lambda: NOW,
        ))
        with self.subTest(stage="database_reopen"), TestClient(app) as client:
            reopened = self._read_priority_without_writes(client=client)
            self.assertEqual(reopened["coverage_percent"], 100)
            self.assertEqual(reopened, final_body)

    def test_added_factors_and_reviewed_weights_do_not_hide_missing_expiration(self):
        self._set_coverage_signals(work_arrangement=True, expiration=False)
        self._apply_coverage_feedback("BAD_SALARY")
        self._apply_coverage_feedback("TOO_SENIOR")

        body = self._read_priority_without_writes()

        self.assertGreater(body["coverage_percent"], 0)
        self.assertLess(body["coverage_percent"], 100)
        self.assertEqual(body["coverage_percent"], 96.15)
        self.assertEqual(body["unknowns"], ["expiration_risk"])
        factors = {row["key"]: row for row in body["factors"]}
        expiration = factors["expiration_risk"]
        self.assertEqual(expiration["status"], "UNKNOWN")
        self.assertIsNone(expiration["score"])
        self.assertIsNone(expiration["contribution"])
        self.assertTrue(all(row["status"] == "KNOWN" for key, row in factors.items()
                            if key != "expiration_risk"))

    def test_priority_explanation_composes_current_signals_without_changing_match_score(self):
        evidence = [self._evidence()]
        attributes = {
            "reported_posted_at": "2026-10-08T12:00:00Z",
            "expires_at": "2026-10-12T12:00:00Z",
            "application_complexity": "MEDIUM",
            "recruiter_classification_evidence": evidence,
        }
        self._set_job_signals(
            attributes=attributes, components=self._components(), postings=(
                "2026-10-07T12:00:00Z", "2026-10-08T12:00:00Z",
            ),
        )
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["job_id"], "job")
        self.assertEqual(body["match_score"], 80.0)
        self.assertIsInstance(body["priority_score"], float)
        self.assertNotEqual(body["priority_score"], body["match_score"])
        self.assertIn(body["priority_band"], {"LOW", "NORMAL", "HIGH", "UNKNOWN"})
        self.assertEqual(body["coverage_percent"], 90)
        self.assertEqual(body["unknowns"], ["company_preference"])
        self.assertTrue(body["advisory"])
        self.assertFalse(body["affects_match_score"])
        factors = {factor["key"]: factor for factor in body["factors"]}
        self.assertEqual(factors["salary"]["status"], "KNOWN")
        self.assertEqual(factors["direct_employer_availability"]["score"], 75.0)
        self.assertEqual(factors["application_complexity"]["status"], "KNOWN")
        self.assertEqual(factors["source_freshness"]["status"], "KNOWN")
        self.assertEqual(factors["company_preference"]["status"], "UNKNOWN")
        self.assertTrue(all(factor["explanation"] and factor["source"]
                            for factor in body["factors"]))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(before[m.matches.name][0]["score"], 80.0)
        self.assertEqual(before[m.applications.name], [])
        self.assertEqual(before[m.operations.name], [])

    def test_reviewed_feedback_changes_only_explainable_priority_not_hard_rules_or_match_score(self):
        preferences_before = self.store.profile("fixture")["preferences"]
        self._set_job_signals(
            attributes={},
            components={"fit_factors": [{"id": "salary", "status": "ALIGNS"}]},
        )
        before = self.client.get(self._url()).json()
        saved_feedback = self.client.post(
            "/profiles/fixture/jobs/job/match-feedback",
            json={"category": "BAD_SALARY", "request_id": "priority-feedback"},
        )
        self.assertEqual(saved_feedback.status_code, 200, saved_feedback.text)
        proposal = saved_feedback.json()["preference_proposal"]
        applied = self.client.post("/profiles/fixture/match-preferences", json={
            "feedback_id": saved_feedback.json()["feedback"]["id"],
            "dimension": proposal["dimension"],
            "direction": proposal["direction"],
            "request_id": "priority-feedback-apply",
        })
        self.assertEqual(applied.status_code, 200, applied.text)

        after = self.client.get(self._url()).json()
        before_salary = next(row for row in before["factors"] if row["key"] == "salary")
        after_salary = next(row for row in after["factors"] if row["key"] == "salary")
        self.assertGreater(after_salary["contribution"], before_salary["contribution"])
        self.assertIn("user-reviewed", after_salary["explanation"].lower())
        self.assertGreater(after["priority_score"], before["priority_score"])
        self.assertEqual(after["match_score"], before["match_score"])
        self.assertFalse(after["affects_match_score"])
        self.assertTrue(after["advisory"])
        self.assertEqual(self.store.profile("fixture")["preferences"], preferences_before)
        matches = self._snapshot()[m.matches.name]
        self.assertEqual(matches[0]["score"], 80.0)

    def test_reviewed_company_feedback_is_bound_to_its_explicit_company_identity(self):
        other_job = self._seed_job(job_id="other-job")
        before = self.client.get(self._url()).json()
        before_factor = next(row for row in before["factors"] if row["key"] == "company_preference")
        self.assertEqual(before_factor["status"], "UNKNOWN")

        feedback = self.client.post("/profiles/fixture/jobs/job/match-feedback", json={
            "category": "BAD_COMPANY", "request_id": "company-feedback",
        })
        self.assertEqual(feedback.status_code, 200, feedback.text)
        proposal = feedback.json()["preference_proposal"]
        applied = self.client.post("/profiles/fixture/match-preferences", json={
            "feedback_id": feedback.json()["feedback"]["id"],
            "dimension": proposal["dimension"],
            "direction": proposal["direction"],
            "request_id": "company-feedback-apply",
        })
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["preference"]["company_id"], "company-job")

        after = self.client.get(self._url()).json()
        after_factor = next(row for row in after["factors"] if row["key"] == "company_preference")
        self.assertEqual(after_factor["status"], "KNOWN")
        self.assertEqual(after_factor["score"], 20.0)
        self.assertIn("disfavored", after_factor["explanation"].lower())
        other = self.client.get(self._url(job_id=other_job["id"])).json()
        other_factor = next(row for row in other["factors"] if row["key"] == "company_preference")
        self.assertEqual(other_factor["status"], "UNKNOWN")

    def test_reviewed_too_senior_feedback_ranks_only_the_explicit_same_level_advisorially(self):
        preferences_before = self.store.profile("fixture")["preferences"]
        other_job = self._seed_job(
            job_id="mid-level-job", attributes={"experience_level": "MID"})
        self._set_job_signals(
            attributes={"experience_level": "SENIOR"}, components={},
        )
        before = self.client.get(self._url()).json()
        self.assertNotIn("seniority", {factor["key"] for factor in before["factors"]})

        feedback = self.client.post("/profiles/fixture/jobs/job/match-feedback", json={
            "category": "TOO_SENIOR", "request_id": "too-senior-feedback",
        })
        self.assertEqual(feedback.status_code, 200, feedback.text)
        applied = self.client.post("/profiles/fixture/match-preferences", json={
            "feedback_id": feedback.json()["feedback"]["id"],
            "dimension": "seniority", "direction": "increase",
            "request_id": "too-senior-preference",
        })
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["preference"]["seniority_level"], "SENIOR")

        after = self.client.get(self._url()).json()
        seniority = next(factor for factor in after["factors"] if factor["key"] == "seniority")
        self.assertEqual(seniority["status"], "KNOWN")
        self.assertEqual(seniority["score"], 20)
        self.assertEqual(seniority["contribution"], 3)
        self.assertIn("too senior", seniority["explanation"].lower())
        self.assertEqual(after["priority_score"], 57.5)
        self.assertEqual(after["match_score"], before["match_score"])
        other = self.client.get(self._url(job_id=other_job["id"])).json()
        self.assertNotIn("seniority", {factor["key"] for factor in other["factors"]})
        self.assertEqual(self.store.profile("fixture")["preferences"], preferences_before)
        state = self._snapshot()
        self.assertEqual(len(state[m.applications.name]), 0)
        self.assertEqual(
            {row["id"]: row["score"] for row in state[m.matches.name]},
            {"match-fixture-job": 80.0, "match-fixture-mid-level-job": 80.0},
        )

    def test_missing_job_attributes_remain_unknown_instead_of_receiving_fallbacks(self):
        before = self._snapshot()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["match_score"], 80.0)
        self.assertEqual(body["priority_score"], 80.0)
        self.assertEqual(body["priority_band"], "HIGH")
        self.assertEqual(body["coverage_percent"], 25)
        self.assertEqual(set(body["unknowns"]), {
            "posting_age", "salary", "company_preference", "direct_employer_availability",
            "application_complexity", "source_freshness", "expiration_risk",
        })
        factors = {factor["key"]: factor for factor in body["factors"]}
        for name in body["unknowns"]:
            self.assertEqual(factors[name]["status"], "UNKNOWN")
            self.assertIsNone(factors[name]["score"])
            self.assertIsNone(factors[name]["contribution"])
        self.assertEqual(self._snapshot(), before)

    def test_explicit_company_states_drive_explainable_priority_without_changing_match(self):
        before = self.client.get(self._url()).json()
        self.assertEqual(before["match_score"], 80.0)
        statuses = {
            "PREFERRED": 100.0,
            "NEUTRAL": 60.0,
            "BLACKLISTED": 20.0,
        }
        for index, (status, expected_score) in enumerate(statuses.items()):
            with self.subTest(status=status):
                saved = self.client.put(
                    "/profiles/fixture/company-preferences/company-job",
                    json={"status": status, "request_id": f"priority-company-{index}"},
                )
                self.assertEqual(saved.status_code, 200, saved.text)
                result = self.client.get(self._url())
                self.assertEqual(result.status_code, 200, result.text)
                factor = next(row for row in result.json()["factors"]
                              if row["key"] == "company_preference")
                self.assertEqual(factor["status"], "KNOWN")
                self.assertEqual(factor["score"], expected_score)
                self.assertEqual(result.json()["match_score"], 80.0)

    def test_profile_job_binding_and_stale_matches_fail_closed_without_writes(self):
        self._seed_job(job_id="other-job", profile_id="other")
        self._seed_job(job_id="unmatched-job", profile_id="other")
        current = self.store.profile("fixture")
        self._seed_job(job_id="stale-job", profile_version="older-profile-version")
        before = self._snapshot()

        missing_profile = self.client.get(self._url(profile_id="missing"))
        cross_profile = self.client.get(self._url(profile_id="other", job_id="job"))
        missing_match = self.client.get(self._url(job_id="unmatched-job"))
        stale = self.client.get(self._url(job_id="stale-job"))

        self.assertEqual(missing_profile.status_code, 404)
        self.assertEqual(missing_profile.json()["detail"]["code"],
                         "APPLICATION_PRIORITY_PROFILE_NOT_FOUND")
        self.assertEqual(cross_profile.status_code, 404)
        self.assertEqual(cross_profile.json()["detail"]["code"],
                         "APPLICATION_PRIORITY_JOB_NOT_FOUND")
        self.assertEqual(missing_match.status_code, 404)
        self.assertEqual(missing_match.json()["detail"]["code"],
                         "APPLICATION_PRIORITY_JOB_NOT_FOUND")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"],
                         "APPLICATION_PRIORITY_STALE_CONTEXT")
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.store.profile("fixture")["version"], current["version"])


if __name__ == "__main__":
    unittest.main()
