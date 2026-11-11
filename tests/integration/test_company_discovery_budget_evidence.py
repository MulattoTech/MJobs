"""Persist value-free stop and elapsed-budget evidence for company discovery."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.triggers import queue_company_discovery
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class CompanyDiscoveryBudgetEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-company-budget-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'collection.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _run(self, origin, *, timeout_seconds=None):
        queued = queue_company_discovery(self.store, {
            "company_name": "Example Systems Inc.",
            "company_website": origin + "/company",
            "application_url": origin + "/board/apply/A",
        }, "PREVIOUSLY_UNSEEN_COMPANY", "fixture")
        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": origin,
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        with patch.dict(os.environ, environment):
            if timeout_seconds is None:
                result = execute_operation(queued["operation_id"])
            else:
                with patch("workers.tasks.timeout", return_value=timeout_seconds):
                    result = execute_operation(queued["operation_id"])
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == queued["operation_id"])).mappings().one())
            terminal = dict(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == queued["operation_id"],
                m.audit.c.action.in_(("OPERATION_ATTEMPT_FINISHED", "OPERATION_DEAD_LETTERED")),
            )).mappings().one())
        return result, operation, terminal["payload"]

    def _assert_budget(self, payload, stop_reason):
        budget = payload["company_discovery_budget"]
        self.assertEqual(set(budget), {"stop_reason", "budget_consumed_ms"})
        self.assertEqual(budget["stop_reason"], stop_reason)
        self.assertIs(type(budget["budget_consumed_ms"]), int)
        self.assertGreaterEqual(budget["budget_consumed_ms"], 0)
        self.assertNotIn("Example Systems", repr(budget))
        self.assertNotIn("/company", repr(budget))

    def test_completed_fixture_discovery_persists_budget_without_changing_jobs(self):
        with fixture_server() as origin:
            result, operation, payload = self._run(origin)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual({row["requisition"] for row in self.store.jobs("fixture")}, {"A", "B"})
        self.assertEqual(self.store.count("company_job_sources"), 1)
        self._assert_budget(payload, "COMPLETED")

    def test_expired_deadline_persists_timeout_budget_without_import(self):
        result, operation, payload = self._run("http://127.0.0.1:8090", timeout_seconds=0)
        self.assertEqual(result["status"], "DEAD_LETTERED")
        self.assertEqual(result["error_category"], "OPERATION_TIMEOUT")
        self.assertEqual(operation["error"], "OPERATION_TIMEOUT")
        self.assertEqual(self.store.count("company_job_sources"), 0)
        self.assertEqual(self.store.jobs("fixture"), [])
        self._assert_budget(payload, "OPERATION_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
