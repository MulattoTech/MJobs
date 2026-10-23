"""Profile-scoped cost controls and read-only AI funnel planning."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.ai.input_freshness import job_fingerprint
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE


class AICostPolicyApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database_url = f"sqlite:///{(self.root / 'cost.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(copy.deepcopy(PROFILE))
        second = copy.deepcopy(PROFILE)
        second.update(id="other-profile", name="Other Profile", email="other@example.test")
        self.store.save_profile(second)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_local_origin_middleware_preserves_request_guards_and_security_headers(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["content-security-policy"],
                         "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                         "base-uri 'self'; form-action 'self'")

        cross_origin = self.client.put("/profiles/fixture/ai-cost-policy",
                                       headers={"origin": "https://attacker.example"}, json={})
        self.assertEqual(cross_origin.status_code, 403, cross_origin.text)
        self.assertEqual(cross_origin.json()["detail"], "Cross-origin mutation blocked")
        cross_site = self.client.get("/profiles/fixture/ai-cost-policy",
                                     headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(cross_site.status_code, 403, cross_site.text)
        self.assertEqual(cross_site.json()["detail"], "Cross-site personal data access blocked")

    def test_profile_cost_policy_defaults_are_safe_and_persist_independently(self):
        default = {"minimum_score": 75, "max_input_candidates": 5000,
                   "max_ai_candidates": 75, "max_document_candidates": 20}
        first = self.client.get("/profiles/fixture/ai-cost-policy")
        second = self.client.get("/profiles/other-profile/ai-cost-policy")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["policy"], default)
        self.assertEqual(second.json()["policy"], default)

        custom = {"minimum_score": 84, "max_input_candidates": 120,
                  "max_ai_candidates": 12, "max_document_candidates": 4}
        saved = self.client.put("/profiles/fixture/ai-cost-policy", json=custom)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["policy"], custom)
        self.assertEqual(self.client.get("/profiles/other-profile/ai-cost-policy").json()["policy"], default)

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        reopened = self.client.get("/profiles/fixture/ai-cost-policy")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["policy"], custom)

    def test_effective_cost_policy_change_appends_one_value_free_audit_event(self):
        default = {"minimum_score": 75, "max_input_candidates": 5000,
                   "max_ai_candidates": 75, "max_document_candidates": 20}
        changed = {"minimum_score": 74, "max_input_candidates": 5000,
                   "max_ai_candidates": 75, "max_document_candidates": 20}

        initial = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertFalse(any(event["type"] == "SETTING_CHANGED"
                             for event in initial.json()["events"]))

        default_save = self.client.put("/profiles/fixture/ai-cost-policy", json=default)
        self.assertEqual(default_save.status_code, 200, default_save.text)
        after_default = self.client.get("/profiles/fixture/audit-history").json()["events"]
        self.assertFalse(any(event["type"] == "SETTING_CHANGED" for event in after_default))

        changed_save = self.client.put("/profiles/fixture/ai-cost-policy", json=changed)
        self.assertEqual(changed_save.status_code, 200, changed_save.text)
        history = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(history.status_code, 200, history.text)
        setting_events = [event for event in history.json()["events"]
                          if event["type"] == "SETTING_CHANGED"]
        self.assertEqual(len(setting_events), 1, history.text)
        self.assertEqual(setting_events[0]["summary"], "A profile setting changed")
        self.assertEqual(set(setting_events[0]), {
            "id", "type", "summary", "subject", "company", "timestamp",
        })

        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == "fixture",
                m.audit.c.action == "SETTING_CHANGED",
            )).all()
        self.assertEqual([row[0] for row in rows], [{}])

        other_history = self.client.get("/profiles/other-profile/audit-history")
        self.assertEqual(other_history.status_code, 200, other_history.text)
        self.assertFalse(any(event["type"] == "SETTING_CHANGED"
                             for event in other_history.json()["events"]))

        repeated = self.client.put("/profiles/fixture/ai-cost-policy", json=changed)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        after_repeat = self.client.get("/profiles/fixture/audit-history").json()["events"]
        self.assertEqual([event["id"] for event in after_repeat
                          if event["type"] == "SETTING_CHANGED"],
                         [setting_events[0]["id"]])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        reopened = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        reopened_settings = [event for event in reopened.json()["events"]
                             if event["type"] == "SETTING_CHANGED"]
        self.assertEqual([event["id"] for event in reopened_settings],
                         [setting_events[0]["id"]])

    def test_invalid_or_secret_bearing_policy_is_rejected_without_echo(self):
        for value in (
            {"minimum_score": 101, "max_input_candidates": 5000,
             "max_ai_candidates": 75, "max_document_candidates": 20},
            {"minimum_score": 70, "max_input_candidates": 5000,
             "max_ai_candidates": 2, "max_document_candidates": 3},
            {"minimum_score": 70, "max_input_candidates": 5000,
             "max_ai_candidates": 76, "max_document_candidates": 20},
            {"minimum_score": 70, "max_input_candidates": 5000,
             "max_ai_candidates": 75, "max_document_candidates": 21},
            {"minimum_score": 70, "max_input_candidates": 5000,
             "max_ai_candidates": 75, "max_document_candidates": 20,
             "api_key": "private-synthetic-secret"},
        ):
            response = self.client.put("/profiles/fixture/ai-cost-policy", json=value)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("private-synthetic-secret", response.text)
            self.assertNotIn("api_key", response.text)

    def test_preview_uses_saved_caps_and_threshold_without_calling_a_provider(self):
        for index, description in enumerate(("Python SQL", "Python SQL", "Terraform")):
            response = self.client.post("/jobs/manual", json={
                "request_id": f"cost-preview-{index}",
                "title": f"Infrastructure role {index}",
                "company": "Synthetic Company",
                "description": description,
                "reference_url": f"https://jobs.example.test/role-{index}",
                "application_url": f"https://apply.example.test/role-{index}",
                "source_details": {},
            })
            self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")

        policy = {"minimum_score": 50, "max_input_candidates": 20,
                  "max_ai_candidates": 1, "max_document_candidates": 0}
        saved = self.client.put("/profiles/fixture/ai-cost-policy", json=policy)
        self.assertEqual(saved.status_code, 200, saved.text)
        preview = self.client.get("/profiles/fixture/ai-cost-plan")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["plan"], {
            "scanned_job_count": 3,
            "scored_candidate_count": 3,
            "unscored_job_count": 0,
            "unique_count": 3,
            "eligible_count": 2,
            "ai_candidate_count": 1,
            "document_candidate_count": 0,
        })
        self.assertTrue(preview.json()["no_model_calls"])

        bounded = {**policy, "max_input_candidates": 2}
        self.assertEqual(self.client.put("/profiles/fixture/ai-cost-policy",
                                         json=bounded).status_code, 200)
        overflow = self.client.get("/profiles/fixture/ai-cost-plan")
        self.assertEqual(overflow.status_code, 422)
        self.assertEqual(overflow.json()["detail"]["code"], "AI_FUNNEL_INPUT_LIMIT")

        lowest_job = self.store.jobs(profile_id="fixture")[-1]
        with self.store.engine.begin() as connection:
            connection.execute(delete(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.job_id == lowest_job["id"],
            ))
        include_all = {"minimum_score": 0, "max_input_candidates": 20,
                       "max_ai_candidates": 75, "max_document_candidates": 20}
        self.assertEqual(self.client.put("/profiles/fixture/ai-cost-policy",
                                         json=include_all).status_code, 200)
        with_unknown = self.client.get("/profiles/fixture/ai-cost-plan")
        self.assertEqual(with_unknown.status_code, 200, with_unknown.text)
        self.assertEqual(with_unknown.json()["plan"]["scanned_job_count"], 3)
        # A missing cached row does not erase evidence in the current posting.
        self.assertEqual(with_unknown.json()["plan"]["scored_candidate_count"], 3)
        self.assertEqual(with_unknown.json()["plan"]["eligible_count"], 3)

        first_job = self.store.jobs(profile_id="fixture")[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == first_job["id"]).values(
                skills=["Python", "SQL", "Kubernetes"], attributes={"skill_evidence": "structured_required_skills"}))
        # Two of three required skills yield66.67%; the bounded integer score is66.
        boundary_policy = {"minimum_score": 67, "max_input_candidates": 20,
                           "max_ai_candidates": 75, "max_document_candidates": 20}
        self.assertEqual(self.client.put("/profiles/fixture/ai-cost-policy",
                                         json=boundary_policy).status_code, 200)
        boundary = self.client.get("/profiles/fixture/ai-cost-plan")
        self.assertEqual(boundary.status_code, 200, boundary.text)
        self.assertEqual(boundary.json()["plan"]["eligible_count"], 1,
                         f"policy={boundary.json().get('policy')} scores={[(row['id'], row['match_score']) for row in self.store.jobs(profile_id='fixture')]}")



    def test_reviewed_document_candidates_are_capped_persisted_and_required_for_preparation(self):
        for index in range(2):
            response = self.client.post("/jobs/manual", json={
                "request_id": f"cost-document-review-{index}",
                "title": f"Reviewed infrastructure role {index}",
                "company": "Synthetic Company",
                "description": f"Windows Server administration role {index}.",
                "reference_url": f"https://jobs.example.test/document-review-{index}",
                "application_url": f"https://apply.example.test/document-review-{index}",
                "source_details": {},
            })
            self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")
        jobs = self.store.jobs(profile_id="fixture")
        job_ids = [job["id"] for job in jobs]
        policy = {"minimum_score": 0, "max_input_candidates": 20,
                  "max_ai_candidates": 2, "max_document_candidates": 1}
        self.store.save_ai_cost_policy("fixture", policy)
        profile_version = self.store.profile("fixture")["version"]
        run = self.store.save_ai_evaluation_run(
            "fixture", profile_version, policy, "ollama", "llama3.2:3b", [
                {"job_id": job_id, "title": job["title"], "company": job["company"],
                 "deterministic_score": 90 - index, "category": "STRONG",
                 "rationale": "Review the original posting before preparation."}
                for index, (job_id, job) in enumerate(zip(job_ids, jobs))
            ], job_fingerprints={job['id']: job_fingerprint(job) for job in jobs})

        cross_profile = self.client.post(
            f"/profiles/other-profile/ai-cost-plan/evaluations/{run['run_id']}/review-documents",
            json={"job_ids": [job_ids[0]]})
        self.assertEqual(cross_profile.status_code, 404, cross_profile.text)

        rejected = self.client.post("/applications", json={
            "profile_id": "fixture", "job_id": job_ids[1],
            "ai_evaluation_run_id": run["run_id"],
        })
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["detail"], "AI_DOCUMENT_CANDIDATE_NOT_REVIEWED")

        reviewed = self.client.post(
            f"/profiles/fixture/ai-cost-plan/evaluations/{run['run_id']}/review-documents",
            json={"job_ids": [job_ids[0]]})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["reviewed_job_ids"], [job_ids[0]])

        over_cap = self.client.post(
            f"/profiles/fixture/ai-cost-plan/evaluations/{run['run_id']}/review-documents",
            json={"job_ids": [job_ids[1]]})
        self.assertEqual(over_cap.status_code, 409, over_cap.text)
        self.assertEqual(over_cap.json()["detail"]["code"], "AI_DOCUMENT_CANDIDATE_LIMIT")

        prepared = self.client.post("/applications", json={
            "profile_id": "fixture", "job_id": job_ids[0],
            "ai_evaluation_run_id": run["run_id"],
        })
        self.assertEqual(prepared.status_code, 202, prepared.text)
        self.assertEqual(prepared.json()["state"], "QUEUED")
        history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations").json()["runs"]
        self.assertTrue(next(row for row in history[0]["evaluations"]
                             if row["job_id"] == job_ids[0])["document_reviewed"])

    def test_document_review_rejects_evaluation_after_profile_changes(self):
        response = self.client.post("/jobs/manual", json={
            "request_id": "cost-document-review-stale",
            "title": "Infrastructure role", "company": "Synthetic Company",
            "description": "Windows Server operations.",
            "reference_url": "https://jobs.example.test/document-review-stale",
            "application_url": "https://apply.example.test/document-review-stale",
            "source_details": {},
        })
        self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")
        job = self.store.jobs(profile_id="fixture")[0]
        policy = {"minimum_score": 0, "max_input_candidates": 20,
                  "max_ai_candidates": 1, "max_document_candidates": 1}
        run = self.store.save_ai_evaluation_run(
            "fixture", self.store.profile("fixture")["version"], policy,
            "ollama", "llama3.2:3b", [{
                "job_id": job["id"], "title": job["title"], "company": job["company"],
                "deterministic_score": 90, "category": "STRONG",
                "rationale": "Check the actual role requirements."}])
        changed = copy.deepcopy(PROFILE)
        changed["name"] = "Changed after evaluation"
        self.store.save_profile(changed)

        response = self.client.post(
            f"/profiles/fixture/ai-cost-plan/evaluations/{run['run_id']}/review-documents",
            json={"job_ids": [job["id"]]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "AI_EVALUATION_PROFILE_CHANGED")










if __name__ == "__main__":
    unittest.main()
