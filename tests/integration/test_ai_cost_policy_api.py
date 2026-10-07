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

    def test_local_origin_middleware_preserves_request_guards_and_security_headers(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["content-security-policy"],
                         "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

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
        with self.store.engine.connect() as connection:
            operation_state = connection.execute(select(m.operations.c.state).where(
                m.operations.c.kind == "AI_EVALUATE")).scalar_one()
        self.assertEqual(operation_state, "COMPLETED")

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

    def test_ai_evaluation_concurrency_limit_is_shared_across_api_instances(self):
        import os
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from services.ai.consumer import AIJobEvaluation
        from services.ai.providers import AIProviderError
        from services.ai.tasks import AI_TASKS
        import time

        for profile_id, request_id in (("fixture", "ai-admission-first"),
                                        ("other-profile", "ai-admission-second")):
            self.assertEqual(self.client.post("/jobs/manual", json={
                "request_id": request_id,
                "title": "Infrastructure Administrator",
                "company": "Synthetic Company",
                "description": "Windows Server operations.",
                "reference_url": f"https://jobs.example.test/{request_id}",
                "application_url": f"https://apply.example.test/{request_id}",
                "source_details": {},
            }).status_code, 201)
            recalculate(self.store, profile_id)
            self.store.save_ai_cost_policy(profile_id, {
                "minimum_score": 0, "max_input_candidates": 20,
                "max_ai_candidates": 5, "max_document_candidates": 0,
            })
            self.store.save_ai_task_settings(profile_id, {
                task: {"provider": "ollama", "model": "synthetic"}
                if task == "job_scoring" else {"provider": "disabled", "model": None}
                for task in AI_TASKS
            })

        second_store = Store(self.database_url)
        second_client = TestClient(create_app(
            second_store, "http://127.0.0.1:8090", self.root,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL"))
        provider_started = threading.Event()
        release_provider = threading.Event()
        calls = []

        class BlockingRouter:
            def __init__(self, configuration):
                self.configuration = configuration

            def generate_structured(self, **_kwargs):
                calls.append(True)
                if len(calls) == 1:
                    provider_started.set()
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if _kwargs["cancellation_event"].is_set():
                            raise AIProviderError("AI_PROVIDER_CANCELLED")
                        if release_provider.wait(0.01):
                            break
                    else:
                        raise TimeoutError("synthetic provider release timed out")
                return AIJobEvaluation(category="STRONG", rationale="Synthetic result.")

        try:
            with patch.dict(os.environ, {"MJOBS_AI_CONCURRENCY": "1"}), \
                    patch("apps.api.main.AITaskRouter", BlockingRouter), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                first = executor.submit(
                    self.client.post, "/profiles/fixture/ai-cost-plan/evaluate",
                    json={"request_id": "shared-capacity-first"})
                self.assertTrue(provider_started.wait(2), "first provider call did not start")

                rejected = second_client.post(
                    "/profiles/other-profile/ai-cost-plan/evaluate",
                    json={"request_id": "shared-capacity-second"})
                self.assertEqual(rejected.status_code, 429, rejected.text)
                self.assertEqual(rejected.json()["detail"]["code"], "AI_CONCURRENCY_LIMIT")
                self.assertEqual(len(calls), 1)

                active_operations = second_client.get(
                    "/operations", params={"profile_id": "fixture"}).json()["operations"]
                active = next(row for row in active_operations
                              if row["kind"] == "AI_EVALUATE" and row["state"] == "RUNNING")
                cancellation = second_client.post(f"/operations/{active['id']}/cancel")
                self.assertEqual(cancellation.status_code, 202, cancellation.text)
                self.assertTrue(cancellation.json()["cancel_requested"])

                completed = first.result(timeout=4)
                self.assertEqual(completed.status_code, 200, completed.text)
                self.assertEqual(completed.json(), {
                    "cancelled": True, "request_id": "shared-capacity-first"})
                with self.store.engine.connect() as connection:
                    first_operation = connection.execute(select(m.operations).where(
                        m.operations.c.kind == "AI_EVALUATE")).mappings().one()
                self.assertEqual(first_operation["state"], "CANCELLED")

                with self.store.engine.begin() as connection:
                    connection.execute(m.operations.insert().values(
                        id="stale-inline-ai-operation", kind="AI_EVALUATE",
                        entity_id="fixture", params={}, state="RUNNING", error=None,
                        updated_at="2000-01-01T00:00:00+00:00", attempt_count=1,
                        cancel_requested=False, lease_expires_at="2000-01-01T00:00:00+00:00",
                    ))
                calls_after_first = len(calls)
                reopened = second_client.post(
                    "/profiles/other-profile/ai-cost-plan/evaluate",
                    json={"request_id": "shared-capacity-reopened"})
                self.assertEqual(reopened.status_code, 200, reopened.text)
                self.assertGreater(len(calls), calls_after_first)
                with self.store.engine.connect() as connection:
                    stale_operation = connection.execute(select(m.operations).where(
                        m.operations.c.id == "stale-inline-ai-operation")).mappings().one()
                self.assertEqual(stale_operation["state"], "FAILED")
                self.assertEqual(stale_operation["error"], "LEASE_EXPIRED")
                self.assertEqual(self.client.get(
                    "/profiles/fixture/ai-cost-plan/evaluations").json()["runs"], [])
                self.assertEqual(len(second_client.get(
                    "/profiles/other-profile/ai-cost-plan/evaluations").json()["runs"]), 1)
        finally:
            release_provider.set()
            second_client.close()
            second_store.engine.dispose()

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
            ])

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

    def test_provider_timeout_is_classified_and_does_not_persist_partial_history(self):
        from unittest.mock import patch
        from services.ai.consumer import AIJobEvaluation
        from services.ai.providers import AIProviderError
        from services.ai.tasks import AI_TASKS

        for index in range(2):
            created = self.client.post("/jobs/manual", json={
                "request_id": f"cost-consumer-timeout-{index}",
                "title": f"Infrastructure Administrator {index}",
                "company": "Synthetic Company",
                "description": "Windows Server operations.",
                "reference_url": f"https://jobs.example.test/consumer-timeout-{index}",
                "application_url": f"https://apply.example.test/consumer-timeout-{index}",
                "source_details": {},
            })
            self.assertEqual(created.status_code, 201, created.text)
        recalculate(self.store, "fixture")
        jobs = self.store.jobs(profile_id="fixture", limit=20)
        with self.store.engine.begin() as connection:
            for job in jobs:
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture",
                    m.matches.c.job_id == job["id"],
                ).values(score=95.0))
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "llama3.2:3b"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })

        class TimedOutRouter:
            def __init__(self, configuration):
                self.configuration = configuration
                self.calls = 0

            def generate_structured(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return AIJobEvaluation(category="STRONG", rationale="Synthetic result.")
                raise AIProviderError("AI_PROVIDER_TIMEOUT")

        with patch("apps.api.main.AITaskRouter", TimedOutRouter):
            response = self.client.post("/profiles/fixture/ai-cost-plan/evaluate")

        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["detail"]["code"], "AI_PROVIDER_TIMEOUT")
        history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
        self.assertEqual(history.json()["runs"], [])

    def test_total_batch_deadline_returns_gateway_timeout_without_saving_results(self):
        from unittest.mock import patch
        from services.ai.consumer import AIJobEvaluation, consume_ai_candidates
        from services.ai.tasks import AI_TASKS

        created = self.client.post("/jobs/manual", json={
            "request_id": "cost-consumer-total-timeout",
            "title": "Infrastructure Administrator",
            "company": "Synthetic Company",
            "description": "Windows Server operations.",
            "reference_url": "https://jobs.example.test/consumer-total-timeout",
            "application_url": "https://apply.example.test/consumer-total-timeout",
            "source_details": {},
        })
        self.assertEqual(created.status_code, 201, created.text)
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

        class SlowRouter:
            def __init__(self, configuration):
                self.configuration = configuration

            def generate_structured(self, **_kwargs):
                return AIJobEvaluation(category="STRONG", rationale="Synthetic result.")

        times = iter([0, 0, 121])

        def consume_after_deadline(*args, **kwargs):
            return consume_ai_candidates(*args, **kwargs, clock=lambda: next(times))

        with patch("apps.api.main.AITaskRouter", SlowRouter), patch(
                "apps.api.main.consume_ai_candidates", side_effect=consume_after_deadline):
            response = self.client.post("/profiles/fixture/ai-cost-plan/evaluate")

        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["detail"]["code"], "AI_TASK_TIMEOUT")
        history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
        self.assertEqual(history.json()["runs"], [])

    def test_disconnect_during_provider_call_cancels_and_does_not_save_partial_run(self):
        import asyncio
        import inspect
        import threading
        from unittest.mock import patch
        from fastapi.responses import JSONResponse
        from services.ai.consumer import AIJobEvaluation
        from services.ai.tasks import AI_TASKS

        created = self.client.post("/jobs/manual", json={
            "request_id": "cost-consumer-disconnect",
            "title": "Infrastructure Administrator",
            "company": "Synthetic Company",
            "description": "Windows Server operations.",
            "reference_url": "https://jobs.example.test/consumer-disconnect",
            "application_url": "https://apply.example.test/consumer-disconnect",
            "source_details": {},
        })
        self.assertEqual(created.status_code, 201, created.text)
        second_created = self.client.post("/jobs/manual", json={
            "request_id": "cost-consumer-disconnect-second",
            "title": "Systems Administrator",
            "company": "Synthetic Company",
            "description": "Linux and infrastructure operations.",
            "reference_url": "https://jobs.example.test/consumer-disconnect-second",
            "application_url": "https://apply.example.test/consumer-disconnect-second",
            "source_details": {},
        })
        self.assertEqual(second_created.status_code, 201, second_created.text)
        recalculate(self.store, "fixture")
        jobs = self.store.jobs(profile_id="fixture")
        with self.store.engine.begin() as connection:
            for job in jobs:
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture",
                    m.matches.c.job_id == job["id"],
                ).values(score=95.0))
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "llama3.2:3b"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })

        route = next(route for route in self.client.app.routes
                     if getattr(route, "path", None) ==
                     "/profiles/{profile_id}/ai-cost-plan/evaluate")
        self.assertTrue(inspect.iscoroutinefunction(route.endpoint))
        request = type("RequestProbe", (), {})()
        provider_started = threading.Event()
        disconnect_observed = threading.Event()
        provider_released = threading.Event()
        provider_calls = []

        async def is_disconnected():
            if provider_started.is_set():
                disconnect_observed.set()
                provider_released.set()
                return True
            return False

        request.is_disconnected = is_disconnected

        class WaitingRouter:
            def __init__(self, configuration):
                self.configuration = configuration
                self.calls = 0

            def generate_structured(self, **_kwargs):
                self.calls += 1
                provider_calls.append(self.calls)
                if self.calls == 1:
                    return AIJobEvaluation(category="STRONG", rationale="Synthetic result.")
                provider_started.set()
                if not provider_released.wait(2):
                    raise AssertionError("disconnect was not observed")
                return AIJobEvaluation(category="STRONG", rationale="Synthetic result.")

        with patch("apps.api.main.AITaskRouter", WaitingRouter):
            response = asyncio.run(route.endpoint("fixture", request))

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 499)
        self.assertEqual(response.body, b'{"detail":{"code":"AI_TASK_CANCELLED"}}')
        self.assertTrue(disconnect_observed.is_set())
        self.assertEqual(provider_calls, [1, 2])
        history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["runs"], [])

    def test_explicit_ai_evaluation_cancel_is_profile_and_request_scoped(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from unittest.mock import patch
        from services.ai.consumer import AIJobEvaluation
        from services.ai.tasks import AI_TASKS

        for index, title in enumerate(("Infrastructure Administrator", "Systems Administrator")):
            response = self.client.post("/jobs/manual", json={
                "request_id": f"explicit-ai-cancel-{index}", "title": title,
                "company": "Synthetic Company", "description": "Windows and Linux infrastructure.",
                "reference_url": f"https://jobs.example.test/explicit-cancel-{index}",
                "application_url": f"https://apply.example.test/explicit-cancel-{index}",
                "source_details": {},
            })
            self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")
        jobs = self.store.jobs(profile_id="fixture")
        with self.store.engine.begin() as connection:
            for job in jobs:
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture",
                    m.matches.c.job_id == job["id"],
                ).values(score=95.0))
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "synthetic-test"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })

        provider_started = threading.Event()
        release_provider = threading.Event()
        provider_calls = []

        class WaitingRouter:
            def __init__(self, _configuration):
                pass

            def generate_structured(self, **_kwargs):
                provider_calls.append(len(provider_calls) + 1)
                if len(provider_calls) == 1:
                    return AIJobEvaluation(category="STRONG", rationale="Synthetic completed result.")
                provider_started.set()
                if not release_provider.wait(60):
                    raise AssertionError("test did not release the bounded provider call")
                return AIJobEvaluation(category="REVIEW", rationale="Synthetic interrupted result.")

        request_id = "fixture-evaluation-request"
        with ThreadPoolExecutor(max_workers=1) as executor, patch(
                "apps.api.main.AITaskRouter", WaitingRouter):
            evaluation = executor.submit(self.client.post,
                "/profiles/fixture/ai-cost-plan/evaluate", json={"request_id": request_id})
            try:
                self.assertTrue(provider_started.wait(5), "second provider call did not start")
                wrong_profile = self.client.post(
                    f"/profiles/other-profile/ai-cost-plan/evaluations/{request_id}/cancel")
                wrong_request = self.client.post(
                    "/profiles/fixture/ai-cost-plan/evaluations/another-request/cancel")
                duplicate_request = self.client.post(
                    "/profiles/fixture/ai-cost-plan/evaluate", json={"request_id": request_id})
                self.assertEqual(wrong_profile.status_code, 200, wrong_profile.text)
                self.assertFalse(wrong_profile.json()["cancelled"])
                self.assertEqual(wrong_request.status_code, 200, wrong_request.text)
                self.assertFalse(wrong_request.json()["cancelled"])
                self.assertEqual(duplicate_request.status_code, 409, duplicate_request.text)
                self.assertEqual(duplicate_request.json()["detail"]["code"],
                                 "AI_EVALUATION_REQUEST_ACTIVE")

                acknowledged = self.client.post(
                    f"/profiles/fixture/ai-cost-plan/evaluations/{request_id}/cancel")
                self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
                self.assertTrue(acknowledged.json()["cancelled"])
                self.assertFalse(evaluation.done(), "cancel acknowledgement must not imply provider completion")
            finally:
                release_provider.set()

            response = evaluation.result(timeout=5)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"cancelled": True, "request_id": request_id})
        self.assertEqual(provider_calls, [1, 2])
        finished_cancel = self.client.post(
            f"/profiles/fixture/ai-cost-plan/evaluations/{request_id}/cancel")
        self.assertEqual(finished_cancel.status_code, 200, finished_cancel.text)
        self.assertFalse(finished_cancel.json()["cancelled"])
        for profile_id in ("fixture", "other-profile"):
            history = self.client.get(f"/profiles/{profile_id}/ai-cost-plan/evaluations")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(history.json()["runs"], [])

    def test_real_http_disconnect_during_provider_call_discards_partial_batch(self):
        import socket
        import threading
        import time
        from unittest.mock import patch
        import uvicorn
        from fixtures.browser_ports import bound_browser_socket
        from services.ai.consumer import AIJobEvaluation
        from services.ai.tasks import AI_TASKS

        for index, title in enumerate(("Infrastructure Administrator", "Systems Administrator")):
            response = self.client.post("/jobs/manual", json={
                "request_id": f"real-disconnect-{index}", "title": title,
                "company": "Synthetic Company", "description": "Windows and Linux infrastructure.",
                "reference_url": f"https://jobs.example.test/real-disconnect-{index}",
                "application_url": f"https://apply.example.test/real-disconnect-{index}",
                "source_details": {},
            })
            self.assertEqual(response.status_code, 201, response.text)
        recalculate(self.store, "fixture")
        jobs = self.store.jobs(profile_id="fixture")
        with self.store.engine.begin() as connection:
            for job in jobs:
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture",
                    m.matches.c.job_id == job["id"],
                ).values(score=95.0))
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "synthetic-test"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })

        provider_started = threading.Event()
        release_provider = threading.Event()
        disconnect_observed = threading.Event()
        disconnect_checks = []
        calls = []

        class WaitingRouter:
            def __init__(self, _configuration):
                pass

            def generate_structured(self, **_kwargs):
                calls.append(len(calls) + 1)
                if len(calls) == 1:
                    return AIJobEvaluation(category="STRONG", rationale="Synthetic result.")
                provider_started.set()
                if not release_provider.wait(5):
                    raise AssertionError("test did not release the bounded in-flight provider call")
                return AIJobEvaluation(category="STRONG", rationale="Synthetic result.")

        from starlette.requests import Request as StarletteRequest
        original_is_disconnected = StarletteRequest.is_disconnected

        async def observe_real_disconnect(request):
            disconnected = await original_is_disconnected(request)
            disconnect_checks.append(disconnected)
            if disconnected:
                disconnect_observed.set()
            return disconnected

        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         operation_mode="PERSONAL")
        sock = bound_browser_socket()
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        peer = None
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    self.fail("AI disconnect transport test API did not start")
                time.sleep(0.02)

            peer = socket.create_connection(("127.0.0.1", sock.getsockname()[1]), timeout=5)
            peer.settimeout(5)
            peer.sendall((f"POST /profiles/fixture/ai-cost-plan/evaluate HTTP/1.1\r\n"
                          f"Host: 127.0.0.1:{sock.getsockname()[1]}\r\n"
                          f"Origin: {origin}\r\nContent-Length: 0\r\n"
                          "Connection: keep-alive\r\n\r\n").encode("ascii"))
            with patch("apps.api.main.AITaskRouter", WaitingRouter), patch.object(
                    StarletteRequest, "is_disconnected", observe_real_disconnect):
                self.assertTrue(provider_started.wait(5),
                                "provider call did not start before client disconnect")
                peer.shutdown(socket.SHUT_WR)
                self.assertTrue(disconnect_observed.wait(3),
                                f"server did not observe the closed TCP transport; checks={disconnect_checks!r}")
                release_provider.set()
                peer.close()
                time.sleep(0.1)
            self.assertEqual(calls, [1, 2])
            history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(history.json()["runs"], [])
        finally:
            release_provider.set()
            if peer is not None:
                peer.close()
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
            self.assertFalse(thread.is_alive(), "AI disconnect transport API did not stop")

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

    def test_explicit_cancel_closes_a_blocked_local_ollama_response(self):
        from concurrent.futures import ThreadPoolExecutor
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import socket
        import threading
        import time
        from unittest.mock import patch
        from sqlalchemy import update
        from services.ai.consumer import consume_ai_candidates as original_consume
        from services.ai.providers import OllamaProvider
        from services.ai.tasks import AI_TASKS, AITaskRouter

        captured = self.client.post("/jobs/manual", json={
            "request_id": "blocked-ollama-cancel-job", "title": "Infrastructure Administrator",
            "company": "Synthetic Company", "description": "Windows and Linux infrastructure.",
            "reference_url": "https://jobs.example.test/blocked-ollama-cancel",
            "application_url": "https://apply.example.test/blocked-ollama-cancel",
            "source_details": {},
        })
        self.assertEqual(captured.status_code, 201, captured.text)
        recalculate(self.store, "fixture")
        jobs = self.store.jobs(profile_id="fixture")
        with self.store.engine.begin() as connection:
            for job in jobs:
                connection.execute(update(m.matches).where(
                    m.matches.c.profile_id == "fixture",
                    m.matches.c.job_id == job["id"],
                ).values(score=95.0))
        self.store.save_ai_task_settings("fixture", {
            task: {"provider": "ollama", "model": "synthetic-test"}
            if task == "job_scoring" else {"provider": "disabled", "model": None}
            for task in AI_TASKS
        })

        provider_started = threading.Event()
        provider_connection_closed = threading.Event()
        finish_response = threading.Event()
        response_body = (
            b'{"model":"synthetic-test","response":"{\\"category\\":'
            b'\\"STRONG\\",\\"rationale\\":\\"Synthetic result.\\"}","done":true}'
        )

        class BlockingOllamaHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                provider_started.set()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body[:1])
                self.wfile.flush()
                self.connection.settimeout(0.05)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not finish_response.is_set():
                    try:
                        if self.connection.recv(1) == b"":
                            provider_connection_closed.set()
                            return
                    except socket.timeout:
                        continue
                    except OSError:
                        provider_connection_closed.set()
                        return
                if finish_response.is_set():
                    try:
                        self.wfile.write(response_body[1:])
                        self.wfile.flush()
                    except OSError:
                        provider_connection_closed.set()

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), BlockingOllamaHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        provider = OllamaProvider(base_url=f"http://127.0.0.1:{server.server_port}")

        def router_factory(configuration):
            return AITaskRouter(configuration, provider_factory=lambda _name: provider)

        def short_bounded_consume(*args, **kwargs):
            kwargs["timeout_seconds"] = 3
            kwargs["total_timeout_seconds"] = 4
            return original_consume(*args, **kwargs)

        request_id = "blocked-ollama-evaluation"
        evaluation = None
        try:
            with ThreadPoolExecutor(max_workers=1) as executor, \
                    patch("apps.api.main.AITaskRouter", router_factory), \
                    patch("apps.api.main.consume_ai_candidates", short_bounded_consume):
                evaluation = executor.submit(
                    self.client.post, "/profiles/fixture/ai-cost-plan/evaluate",
                    json={"request_id": request_id},
                )
                self.assertTrue(provider_started.wait(3), "local Ollama request did not start")
                acknowledged = self.client.post(
                    f"/profiles/fixture/ai-cost-plan/evaluations/{request_id}/cancel")
                self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
                self.assertTrue(acknowledged.json()["cancelled"])
                self.assertTrue(provider_connection_closed.wait(1),
                                "cancellation did not close the stalled Ollama connection promptly")
                response = evaluation.result(timeout=4)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"cancelled": True, "request_id": request_id})
            history = self.client.get("/profiles/fixture/ai-cost-plan/evaluations")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(history.json()["runs"], [])
        finally:
            finish_response.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
