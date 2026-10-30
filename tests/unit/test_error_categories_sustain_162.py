"""Public AI operation failures expose only the canonical error category."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.ai.durable_evaluation import DurableEvaluationError
from services.ai.providers import AIProviderError
from services.discovery.matching import recalculate
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class AIOperationErrorCategoryTests(unittest.TestCase):
    def test_worker_ai_diagnostics_are_generic_in_result_audit_and_public_readback(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-ai-error-category-") as directory:
            root = Path(directory)
            url = "sqlite:///" + (root / "operations.db").as_posix()
            store = Store(url)
            store.migrate()
            store.bind_workspace_mode("PERSONAL")
            store.save_profile(dict(PROFILE))
            client = TestClient(create_app(store, "http://127.0.0.1:1", root,
                                           operation_mode="PERSONAL"))
            try:
                response = client.post("/jobs/manual", json={
                    "request_id": "durable-ai-synthetic-job",
                    "title": "Infrastructure engineer",
                    "company": "Synthetic Company",
                    "description": "Python SQL synthetic private sentinel.",
                    "reference_url": "https://jobs.example.test/durable-ai",
                    "application_url": "https://apply.example.test/durable-ai",
                    "source_details": {},
                })
                self.assertEqual(response.status_code, 201, response.text)
                recalculate(store, "fixture")
                with store.engine.begin() as connection:
                    connection.execute(update(m.matches).where(
                        m.matches.c.profile_id == "fixture").values(score=99))
                store.save_ai_task_settings(
                    "fixture", {"job_scoring": {"provider": "ollama", "model": "fixture"}})

                failures = (
                    ("timeout", AIProviderError("AI_PROVIDER_TIMEOUT"), None),
                    ("unknown-secret", DurableEvaluationError(
                        "Authorization: Bearer synthetic-secret-value"),
                     "synthetic-secret-value"),
                )
                for label, failure, secret in failures:
                    with self.subTest(label=label):
                        queued = client.post("/profiles/fixture/ai-cost-plan/evaluate/queued",
                                             json={"request_id": f"ai-error-category-{label}-0001"})
                        self.assertEqual(queued.status_code, 202, queued.text)
                        operation_id = queued.json()["operation_id"]

                        class FailingRouter:
                            def __init__(self, _configuration):
                                pass

                            def generate_structured(self, **_kwargs):
                                raise failure

                        with patch("services.ai.durable_evaluation.AITaskRouter", FailingRouter), \
                                patch.dict(os.environ, {
                                    "MJOBS_DATABASE_URL": url,
                                    "MJOBS_OPERATION_MODE": "PERSONAL",
                                }):
                            result = execute_operation(operation_id)

                        self.assertEqual(result["status"], "BLOCKED")
                        self.assertEqual(result["error_category"], "AI_ERROR")
                        reopened = Store(url)
                        try:
                            with reopened.engine.connect() as connection:
                                operation = connection.execute(select(m.operations).where(
                                    m.operations.c.id == operation_id)).mappings().one()
                                audit = list(connection.execute(select(m.audit.c.payload).where(
                                    m.audit.c.entity_id == operation_id,
                                    m.audit.c.action == "OPERATION_ATTEMPT_FINISHED")).scalars())
                            self.assertEqual(operation["error"], "AI_ERROR")
                            self.assertEqual(len(audit), 1)
                            self.assertEqual(audit[0]["error"], "AI_ERROR")
                            public = TestClient(create_app(reopened, "http://127.0.0.1:1", root,
                                                           operation_mode="PERSONAL"))
                            try:
                                readback = public.get(f"/operations/{operation_id}")
                                self.assertEqual(readback.status_code, 200, readback.text)
                                self.assertEqual(readback.json()["error"], "AI_ERROR")
                                if secret:
                                    observable = json.dumps({"result": result, "operation": dict(operation),
                                                            "audit": audit, "readback": readback.json()})
                                    self.assertNotIn(secret, observable)
                            finally:
                                public.close()
                        finally:
                            reopened.engine.dispose()
            finally:
                client.close()
                store.engine.dispose()
