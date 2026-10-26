"""MJ-REF-033: public DISCOVER enforces bounded collection acceptance."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source
from services.discovery.sources.contract import (
    CollectionStrategy,
    SearchBatch,
    SourceCapabilities,
    SourceCapability,
    SourceCompany,
    SourceItem,
)
from services.discovery.sources.errors import SourceError
from services.discovery.sources.registry import SourceAdapterRegistry
from services.discovery.sources.remoteok import RemoteOK
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class _BudgetedFixtureAdapter(RemoteOK):
    def __init__(self, check, state):
        super().__init__(transport=None, check=check)
        self.state = state

    def capabilities(self):
        return SourceCapabilities(
            strategy=CollectionStrategy.BROWSER,
            features=frozenset({SourceCapability.DISCOVERY}),
        )

    def search(self):
        items = self.state["items"]
        for _ in range(self.state["pages"]):
            self.check.consume_page()
        if self.state.get("escape"):
            raise SourceError(self.state["escape"])
        return SearchBatch(
            endpoint="https://fixture.invalid/jobs",
            items=tuple(items),
            complete_inventory=self.state.get("complete_inventory", True),
        )


class AgentBrowserBudgetEntrypointSustain403Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-agent-budget-403-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'collection.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        company = SourceCompany(
            identity_parts=("fixture-budget", "example-company"),
            name="Example Company",
            domain="https://example.invalid",
        )
        self.items = (
            SourceItem({
                "id": 40301,
                "position": "Systems Administrator",
                "company": "Example Company",
                "location": "Remote",
                "description": "Maintain bounded synthetic systems.",
                "url": "https://fixture.invalid/jobs/40301",
            }, company),
            SourceItem({
                "id": 40302,
                "position": "Platform Administrator",
                "company": "Example Company",
                "location": "Remote",
                "description": "Validate bounded synthetic collections.",
                "url": "https://fixture.invalid/jobs/40302",
            }, company),
        )
        self.state = {"items": self.items, "pages": 2}

        def factory(_source, *, transport=None, check=None):
            self.assertIsNone(transport)
            return _BudgetedFixtureAdapter(check, self.state)

        self.registry = SourceAdapterRegistry((("fixture_budget", factory),))
        self.source_id = configure_source(
            self.store,
            "bounded-browser",
            provider="fixture_budget",
            registry=self.registry,
        )
        self.environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_OUTPUT_DIR": str(self.root / "outputs"),
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
        }

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _operation(self, *, expected_count=2, expected_tail=40302):
        request = {
            "profile_id": "fixture",
            "source_id": self.source_id,
            "max_pages": 2,
            "collection_contract": {
                "expected_count": expected_count,
                "tail_key": "id",
                "expected_tail": expected_tail,
                "required_item_fields": ["id", "position", "company"],
            },
        }
        with TestClient(create_app(
                self.store, "http://127.0.0.1:1", self.root,
                operation_mode="FIXTURE", source_registry=self.registry)) as client:
            response = client.post("/discovery", json=request)
        self.assertEqual(response.status_code, 202, response.text)
        operation_id = response.json()["operation_id"]
        with self.store.engine.connect() as connection:
            params = connection.execute(select(m.operations.c.params).where(
                m.operations.c.id == operation_id)).scalar_one()
        self.assertEqual(params["source_id"], self.source_id)
        self.assertEqual(params["max_pages"], 2)
        self.assertEqual(params["collection_contract"], request["collection_contract"])
        return operation_id

    def _execute(self, operation_id):
        with patch.dict(os.environ, self.environment):
            return execute_operation(operation_id, source_registry=self.registry)

    def _saved(self, operation_id):
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())
            terminal = dict(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == operation_id,
                m.audit.c.action.in_(("OPERATION_ATTEMPT_FINISHED", "OPERATION_DEAD_LETTERED")),
            )).mappings().one())
            jobs = list(connection.execute(select(m.jobs.c.id)))
        return operation, terminal, jobs

    def test_public_discover_persists_verified_budget_and_collection_artifact(self):
        operation_id = self._operation()

        result = self._execute(operation_id)

        self.assertEqual(result["status"], "COMPLETED", result)
        operation, terminal, jobs = self._saved(operation_id)
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual(len(jobs), 2)
        evidence = terminal["payload"]["collection_budget"]
        self.assertEqual(evidence["stop_reason"], "SUCCESS_ARTIFACT_VERIFIED")
        self.assertEqual(evidence["max_pages"], 2)
        self.assertEqual(evidence["pages_consumed"], 2)
        self.assertGreaterEqual(evidence["budget_consumed_ms"], 0)
        self.assertEqual(evidence["collection_result"], {
            "code": "OUTPUT_VALID",
            "observed_count": 2,
            "tail_matches": True,
        })
        self.assertNotIn("description", repr(evidence))

    def test_explicit_count_and_tail_anchor_an_incomplete_batch(self):
        self.state.update(complete_inventory=False)
        operation_id = self._operation()

        result = self._execute(operation_id)

        self.assertEqual(result["status"], "COMPLETED", result)
        operation, terminal, jobs = self._saved(operation_id)
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(terminal["payload"]["collection_budget"]["collection_result"], {
            "code": "OUTPUT_VALID",
            "observed_count": 2,
            "tail_matches": True,
        })

    def test_complete_inventory_supplies_authoritative_count_anchor(self):
        operation_id = self._operation(expected_count=None, expected_tail=None)

        result = self._execute(operation_id)

        self.assertEqual(result["status"], "COMPLETED", result)
        operation, terminal, jobs = self._saved(operation_id)
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(terminal["payload"]["collection_budget"]["collection_result"], {
            "code": "OUTPUT_VALID",
            "observed_count": 2,
            "tail_matches": None,
        })

    def test_early_truncation_stops_for_review_before_any_job_is_persisted(self):
        self.state.update(items=self.items[:1], pages=1)
        operation_id = self._operation()

        result = self._execute(operation_id)

        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["error_category"], "REVIEW_REQUIRED", result)
        operation, terminal, jobs = self._saved(operation_id)
        self.assertEqual(operation["state"], "BLOCKED")
        self.assertEqual(jobs, [])
        evidence = terminal["payload"]["collection_budget"]
        self.assertEqual(evidence["stop_reason"], "COUNT_MISMATCH")
        self.assertEqual(evidence["pages_consumed"], 1)
        self.assertEqual(evidence["collection_result"]["observed_count"], 1)

    def test_typed_escape_is_terminal_and_budgeted_without_retry_or_import(self):
        self.state.update(items=(), pages=1, escape="SOURCE_CHANGED")
        operation_id = self._operation()

        result = self._execute(operation_id)

        self.assertEqual(result["status"], "DEAD_LETTERED", result)
        self.assertEqual(result["error_category"], "SOURCE_CHANGED", result)
        operation, terminal, jobs = self._saved(operation_id)
        self.assertEqual(operation["attempt_count"], 1)
        self.assertEqual(operation["retry_count"], 0)
        self.assertEqual(jobs, [])
        evidence = terminal["payload"]["collection_budget"]
        self.assertEqual(evidence["stop_reason"], "SOURCE_CHANGED")
        self.assertEqual(evidence["pages_consumed"], 1)
        self.assertIsNone(evidence["collection_result"])

    def test_page_budget_exhaustion_requires_review_without_poisoning_source_health(self):
        self.state.update(pages=3)
        operation_id = self._operation()
        with self.store.engine.connect() as connection:
            before = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == self.source_id)).mappings().one())

        result = self._execute(operation_id)

        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["error_category"], "REVIEW_REQUIRED", result)
        operation, terminal, jobs = self._saved(operation_id)
        self.assertEqual(operation["attempt_count"], 1)
        self.assertEqual(operation["retry_count"], 0)
        self.assertEqual(jobs, [])
        evidence = terminal["payload"]["collection_budget"]
        self.assertEqual(evidence["stop_reason"], "PAGE_BUDGET_EXHAUSTED")
        self.assertEqual(evidence["max_pages"], 2)
        self.assertEqual(evidence["pages_consumed"], 2)
        self.assertIsNone(evidence["collection_result"])
        with self.store.engine.connect() as connection:
            after = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == self.source_id)).mappings().one())
        for field in ("health_status", "consecutive_failures", "last_error", "backoff_until"):
            self.assertEqual(after[field], before[field], field)

    def test_nonempty_unknown_inventory_requires_review_without_import_or_health_mutation(self):
        self.state.update(items=self.items[:1], pages=1, complete_inventory=False)
        operation_id = self._operation(expected_count=None, expected_tail=None)
        with self.store.engine.connect() as connection:
            before = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == self.source_id)).mappings().one())

        result = self._execute(operation_id)

        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertEqual(result["error_category"], "REVIEW_REQUIRED", result)
        operation, terminal, jobs = self._saved(operation_id)
        self.assertEqual(operation["attempt_count"], 1)
        self.assertEqual(operation["retry_count"], 0)
        self.assertEqual(jobs, [])
        evidence = terminal["payload"]["collection_budget"]
        self.assertEqual(evidence["stop_reason"], "COUNT_UNKNOWN")
        self.assertEqual(evidence["pages_consumed"], 1)
        self.assertEqual(evidence["collection_result"], {
            "code": "COUNT_UNKNOWN",
            "observed_count": None,
            "tail_matches": None,
        })
        with self.store.engine.connect() as connection:
            after = dict(connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == self.source_id)).mappings().one())
        for field in ("health_status", "consecutive_failures", "last_error", "backoff_until"):
            self.assertEqual(after[field], before[field], field)

    def test_public_discover_rejects_unbounded_or_malformed_collection_controls(self):
        valid = {
            "profile_id": "fixture",
            "source_id": self.source_id,
            "max_pages": 2,
            "collection_contract": {
                "expected_count": 2,
                "tail_key": "id",
                "expected_tail": 40302,
                "required_item_fields": ["id", "position", "company"],
            },
        }
        invalid = (
            {**valid, "max_pages": 0},
            {**valid, "max_pages": 101},
            {**valid, "max_pages": 2.5},
            {key: value for key, value in valid.items() if key != "source_id"},
            {key: value for key, value in valid.items() if key != "max_pages"},
            {key: value for key, value in valid.items() if key != "collection_contract"},
            {**valid, "query": "must not be silently ignored"},
            {**valid, "collection_contract": {
                **valid["collection_contract"], "unexpected": "escape"}},
            {**valid, "collection_contract": {
                **valid["collection_contract"], "required_item_fields": ["id", 7]}},
        )
        with TestClient(create_app(
                self.store, "http://127.0.0.1:1", self.root,
                operation_mode="FIXTURE", source_registry=self.registry)) as client:
            for request in invalid:
                with self.subTest(request=request):
                    response = client.post("/discovery", json=request)
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.count("worker_operations"), 0)


if __name__ == "__main__":
    unittest.main()
