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
        self.assertEqual(with_unknown.json()["plan"]["scored_candidate_count"], 2)
        self.assertEqual(with_unknown.json()["plan"]["eligible_count"], 2)

        first_job = self.store.jobs(profile_id="fixture")[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.job_id == first_job["id"],
            ).values(score=74.99))
        boundary_policy = {"minimum_score": 75, "max_input_candidates": 20,
                           "max_ai_candidates": 75, "max_document_candidates": 20}
        self.assertEqual(self.client.put("/profiles/fixture/ai-cost-policy",
                                         json=boundary_policy).status_code, 200)
        boundary = self.client.get("/profiles/fixture/ai-cost-plan")
        self.assertEqual(boundary.status_code, 200, boundary.text)
        self.assertEqual(boundary.json()["plan"]["eligible_count"], 1,
                         f"policy={boundary.json().get('policy')} scores={[(row['id'], row['match_score']) for row in self.store.jobs(profile_id='fixture')]}")

    def test_explicit_evaluate_endpoint_consumes_only_profile_policy_candidates(self):
        from unittest.mock import patch
        from services.ai.consumer import AIJobEvaluation
        from services.ai.tasks import AI_TASKS

        for index in range(3):
            response = self.client.post("/jobs/manual", json={
                "request_id": f"cost-consume-{index}",
                "title": f"Infrastructure role {index}",
                "company": "Synthetic Company",
                "description": f"Windows Server role {index}.",
                "reference_url": f"https://jobs.example.test/consume-{index}",
                "application_url": f"https://apply.example.test/consume-{index}",
                "source_details": {},
            })
            self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")
        with self.store.engine.begin() as connection:
            rows = list(connection.execute(select(m.matches.c.job_id)).mappings())
            for score, row in zip((98.0, 92.0, 40.0), rows):
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture",
                    m.matches.c.job_id == row["job_id"],
                ).values(score=score))
        self.store.save_ai_cost_policy("fixture", {
            "minimum_score": 75, "max_input_candidates": 20,
            "max_ai_candidates": 1, "max_document_candidates": 1,
        })
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "llama3.2:3b"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })
        class Router:
            def __init__(self, configuration):
                self.configuration = configuration
                self.calls = []

            def generate_structured(self, **kwargs):
                self.calls.append(kwargs)
                return AIJobEvaluation(category="STRONG", rationale="Relevant infrastructure role.")

        with patch("apps.api.main.AITaskRouter", Router):
            response = self.client.post("/profiles/fixture/ai-cost-plan/evaluate")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evaluated_candidate_count"], 1)
        self.assertEqual(response.json()["document_candidate_ids"], [])
        self.assertEqual(response.json()["evaluations"][0]["category"], "STRONG")
        self.assertEqual(response.json()["task_provider"], "ollama")
        self.assertEqual(response.json()["task_model"], "llama3.2:3b")
        self.assertEqual(response.json()["policy"]["max_ai_candidates"], 1)
        run_id = response.json()["run_id"]

        history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["runs"][0]["run_id"], run_id)
        self.assertEqual(history.json()["runs"][0]["evaluations"][0]["job_id"],
                         response.json()["evaluations"][0]["job_id"])
        self.assertEqual(self.client.get(
            "/profiles/other-profile/ai-cost-plan/evaluations").json()["runs"], [])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:8090", self.root,
                                            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        reopened = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["runs"][0]["run_id"], run_id)
        self.assertEqual(reopened.json()["runs"][0]["evaluations"][0]["category"], "STRONG")

    def test_structured_provider_failures_are_reported_as_upstream_errors(self):
        from unittest.mock import patch
        from services.ai.structured_output import StructuredOutputError
        from services.ai.tasks import AI_TASKS

        response = self.client.post("/jobs/manual", json={
            "request_id": "cost-consumer-provider-error",
            "title": "Infrastructure Administrator",
            "company": "Synthetic Company",
            "description": "Windows Server operations.",
            "reference_url": "https://jobs.example.test/provider-error",
            "application_url": "https://apply.example.test/provider-error",
            "source_details": {},
        })
        self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")
        job = self.store.jobs(profile_id="fixture")[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.job_id == job["id"],
            ).values(score=95.0))
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "llama3.2:3b"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })

        class FailingRouter:
            def __init__(self, configuration):
                self.configuration = configuration

            def generate_structured(self, **kwargs):
                raise StructuredOutputError("AI_STRUCTURED_PROVIDER_FAILED")

        with patch("apps.api.main.AITaskRouter", FailingRouter):
            response = self.client.post("/profiles/fixture/ai-cost-plan/evaluate")

        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["detail"]["code"], "AI_STRUCTURED_PROVIDER_FAILED")

    def test_results_are_not_saved_when_profile_changes_during_provider_call(self):
        from unittest.mock import patch
        from services.ai.consumer import AIJobEvaluation
        from services.ai.tasks import AI_TASKS

        response = self.client.post("/jobs/manual", json={
            "request_id": "cost-consumer-profile-change",
            "title": "Infrastructure Administrator",
            "company": "Synthetic Company",
            "description": "Windows Server operations.",
            "reference_url": "https://jobs.example.test/profile-change",
            "application_url": "https://apply.example.test/profile-change",
            "source_details": {},
        })
        self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")
        job = self.store.jobs(profile_id="fixture")[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.matches).where(
                m.matches.c.profile_id == "fixture",
                m.matches.c.job_id == job["id"],
            ).values(score=95.0))
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "llama3.2:3b"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })

        class ChangingRouter:
            def __init__(self, configuration):
                self.configuration = configuration

            def generate_structured(self, **kwargs):
                changed = copy.deepcopy(PROFILE)
                changed["name"] = "Changed while inference was running"
                self_outer.store.save_profile(changed)
                return AIJobEvaluation(category="REVIEW", rationale="Review required.")

        self_outer = self
        with patch("apps.api.main.AITaskRouter", ChangingRouter):
            response = self.client.post("/profiles/fixture/ai-cost-plan/evaluate")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "AI_EVALUATION_PROFILE_CHANGED")
        history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["runs"], [])


if __name__ == "__main__":
    unittest.main()
