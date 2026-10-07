"""The local agent boundary exposes typed data, not host capabilities."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from pydantic import BaseModel, ConfigDict

from packages.persistence import models as m
from packages.persistence.store import Store
from services.agents.defaults import build_default_registry
from services.agents.registry import AgentTool, AgentToolContext, AgentToolError, ToolClass, ToolRegistry
from services.agents.runtime import AgentRuntime, ReviewDecision, read_agent_trace
from services.tracking.jobs import read_job_tracking
from tests.integration.test_spine import PROFILE


class AgentToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "agent.db"
        self.url = f"sqlite:///{self.database.as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.save_profile(PROFILE)
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company-one", name="Example", domain="example.test"))
            connection.execute(m.jobs.insert().values(
                id="job-one", company_id="company-one", requisition="REQ-1",
                title="Systems Administrator", description="Manage systems.",
                skills=["Python"], location="Remote", remote_type="REMOTE_US",
                attributes={}))
            connection.execute(m.routes.insert().values(
                id="route-one", job_id="job-one", company_id="company-one", provider="GREENHOUSE",
                tenant="example", requisition="REQ-1",
                url="https://example.test/jobs/REQ-1"))
        self.registry = build_default_registry(self.store)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def runtime(self, task="resume_tailor"):
        return AgentRuntime(self.store, self.registry, profile_id="fixture", task=task)

    def test_profile_read_is_task_scoped_and_trace_is_durable_and_value_free(self):
        result = self.runtime().run([
            {"name": "read_profile_context", "arguments": {
                "requested_paths": ["facts.python.text"]}}
        ], cost_usd=0.0125, input_tokens=14, output_tokens=9)

        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(result["steps"][0]["output"]["context"]["facts.python.text"], "Python")
        reopened = Store(self.url)
        try:
            trace = read_agent_trace(reopened, result["run_id"])
        finally:
            reopened.engine.dispose()
        serialized = json.dumps(trace)
        self.assertEqual(trace["terminal"]["state"], "COMPLETED")
        self.assertEqual(trace["steps"][0]["tool_name"], "read_profile_context")
        self.assertEqual(trace["terminal"]["cost_usd"], 0.0125)
        self.assertEqual(trace["terminal"]["input_tokens"], 14)
        self.assertEqual(trace["terminal"]["output_tokens"], 9)
        self.assertNotIn("Python", serialized)
        self.assertNotIn(PROFILE["email"], serialized)

    def test_registry_exports_only_typed_schemas_and_never_host_handles(self):
        definitions = self.registry.model_schemas()
        serialized = json.dumps(definitions).lower()

        self.assertEqual({item["classification"] for item in definitions}, {"READ", "SIDE_EFFECT"})
        self.assertTrue(all("input_schema" in item for item in definitions))
        for forbidden in ("handler", "store", "connection", "filesystem", "browser", "network", "socket"):
            self.assertNotIn(forbidden, serialized)

    def test_registry_supports_read_propose_and_side_effect_classes(self):
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str

        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str

        registry = ToolRegistry()
        registry.register(AgentTool("read_value", "Read value", ToolClass.READ,
                                    Request, Response, lambda args, context: {"value": args.value}))
        registry.register(AgentTool("propose_value", "Propose value", ToolClass.PROPOSE,
                                    Request, Response, lambda args, context: {"value": args.value}))
        registry.register(AgentTool("write_value", "Reviewed write", ToolClass.SIDE_EFFECT,
                                    Request, Response, lambda args, context: {"value": args.value},
                                    lambda args, context: None))
        self.assertEqual({item["classification"] for item in registry.model_schemas()},
                         {"READ", "PROPOSE", "SIDE_EFFECT"})
        output, proposal = registry.invoke("propose_value", {"value": "safe"},
                                           AgentToolContext("run", "profile", "resume_tailor"))
        self.assertEqual(output, {"value": "safe"})
        self.assertIsNone(proposal)

    def test_profile_read_rejects_unapproved_fields_without_value_leaking_to_trace(self):
        result = self.runtime().run([
            {"name": "read_profile_context", "arguments": {"requested_paths": ["email"]}}
        ])

        self.assertEqual(result["state"], "FAILED")
        trace = read_agent_trace(self.store, result["run_id"])
        self.assertEqual(trace["steps"][0]["result_code"], "TOOL_FAILED")
        self.assertNotIn(PROFILE["email"], json.dumps(trace))

    def test_unregistered_destructive_tool_cannot_change_profile_or_history(self):
        before = self.store.count("audit_events")
        result = self.runtime().run([
            {"name": "delete_profile", "arguments": {"profile_id": "fixture"}}
        ])

        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(result["steps"][0]["result_code"], "UNKNOWN_TOOL")
        self.assertEqual(self.store.profile("fixture")["email"], PROFILE["email"])
        self.assertEqual(self.store.count("audit_events"), before + 3)

    def test_side_effect_waits_for_matching_human_review_and_policy(self):
        runtime = self.runtime("job_match")
        result = runtime.run([
            {"name": "set_job_disposition", "arguments": {
                "job_id": "job-one", "disposition": "SAVED"}}
        ])
        proposal = result["steps"][0]["output"]

        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        self.assertEqual(read_job_tracking(self.store, "fixture", "job-one")["disposition"], "ACTIVE")
        other_runtime = AgentRuntime(self.store, build_default_registry(self.store),
                                     profile_id="other", task="job_match")
        with self.assertRaisesRegex(ValueError, "proposal"):
            other_runtime.review(result["run_id"], proposal["proposal_id"],
                                 "set_job_disposition",
                                 {"job_id": "job-one", "disposition": "SAVED"},
                                 ReviewDecision(reviewer_id="other-user", approved=True))
        wrong_task_runtime = AgentRuntime(self.store, build_default_registry(self.store),
                                          profile_id="fixture", task="resume_tailor")
        with self.assertRaisesRegex(ValueError, "proposal"):
            wrong_task_runtime.review(result["run_id"], proposal["proposal_id"],
                                      "set_job_disposition",
                                      {"job_id": "job-one", "disposition": "SAVED"},
                                      ReviewDecision(reviewer_id="local-user", approved=True))
        with self.assertRaisesRegex(ValueError, "proposal"):
            runtime.review(result["run_id"], proposal["proposal_id"], "set_job_disposition",
                           {"job_id": "job-one", "disposition": "HIDDEN"},
                           ReviewDecision(reviewer_id="local-user", approved=True))
        denied_run = runtime.run([
            {"name": "set_job_disposition", "arguments": {
                "job_id": "job-one", "disposition": "ACTIVE"}}
        ])
        denied_proposal = denied_run["steps"][0]["output"]
        denied = runtime.review(denied_run["run_id"], denied_proposal["proposal_id"],
                                "set_job_disposition",
                                {"job_id": "job-one", "disposition": "ACTIVE"},
                                ReviewDecision(reviewer_id="local-user", approved=True))
        self.assertEqual(denied["result_code"], "POLICY_DENIED")
        self.assertEqual(read_job_tracking(self.store, "fixture", "job-one")["disposition"], "ACTIVE")
        with self.assertRaisesRegex(ValueError, "already reviewed"):
            runtime.review(denied_run["run_id"], denied_proposal["proposal_id"],
                           "set_job_disposition",
                           {"job_id": "job-one", "disposition": "ACTIVE"},
                           ReviewDecision(reviewer_id="local-user", approved=True))

        approved = runtime.review(result["run_id"], proposal["proposal_id"], "set_job_disposition",
                                  {"job_id": "job-one", "disposition": "SAVED"},
                                  ReviewDecision(reviewer_id="local-user", approved=True))
        self.assertEqual(approved["result_code"], "EXECUTED")
        self.assertEqual(read_job_tracking(self.store, "fixture", "job-one")["disposition"], "SAVED")
        with self.assertRaisesRegex(ValueError, "already reviewed"):
            runtime.review(result["run_id"], proposal["proposal_id"], "set_job_disposition",
                           {"job_id": "job-one", "disposition": "SAVED"},
                           ReviewDecision(reviewer_id="local-user", approved=True))

        declined_run = runtime.run([
            {"name": "set_job_disposition", "arguments": {
                "job_id": "job-one", "disposition": "NOT_INTERESTED"}}
        ])
        declined = declined_run["steps"][0]["output"]
        self.assertEqual(runtime.review(declined_run["run_id"], declined["proposal_id"],
                                        "set_job_disposition",
                                        {"job_id": "job-one", "disposition": "NOT_INTERESTED"},
                                        ReviewDecision(reviewer_id="local-user", approved=False))[
                                             "result_code"], "USER_REJECTED")
        with self.assertRaisesRegex(ValueError, "already reviewed"):
            runtime.review(declined_run["run_id"], declined["proposal_id"],
                           "set_job_disposition",
                           {"job_id": "job-one", "disposition": "NOT_INTERESTED"},
                           ReviewDecision(reviewer_id="local-user", approved=True))

    def test_trace_budget_ends_run_without_executing_excess_tool_calls(self):
        result = self.runtime().run([
            {"name": "read_profile_context", "arguments": {
                "requested_paths": ["facts.python.text"]}}
            for _ in range(21)
        ])

        self.assertEqual(result["state"], "LIMIT_REACHED")
        self.assertEqual(len(result["steps"]), 20)
        trace = read_agent_trace(self.store, result["run_id"])
        self.assertEqual(trace["terminal"]["state"], "LIMIT_REACHED")

    def test_wall_clock_budget_stops_before_next_tool_and_persists_consumption(self):
        from services.agents import runtime as runtime_module
        calls = []
        class Clock:
            now = 10.0
            def __call__(self):
                return self.now
        clock = Clock()
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        def handler(args, context):
            calls.append(args.value)
            clock.now += 0.6
            return {"value": args.value}
        registry = ToolRegistry()
        registry.register(AgentTool("read_value", "Read value", ToolClass.READ,
                                    Request, Response, handler))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture", task="resume_tailor")
        with patch.object(runtime_module.time, "monotonic", clock):
            result = runtime.run([
                {"name": "read_value", "arguments": {"value": "first"}},
                {"name": "read_value", "arguments": {"value": "second"}},
            ], max_duration_ms=500)

        self.assertEqual(calls, ["first"])
        self.assertEqual(result["state"], "LIMIT_REACHED")
        trace = read_agent_trace(self.store, result["run_id"])
        self.assertEqual(trace["terminal"]["stop_reason"], "TIME_BUDGET_EXHAUSTED")
        self.assertEqual(trace["terminal"]["budget_consumed_ms"], 600)

    def test_explicit_escape_outcome_is_a_terminal_result(self):
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        registry = ToolRegistry()
        registry.register(AgentTool("read_value", "Read value", ToolClass.READ,
                                    Request, Response,
                                    lambda args, context: (_ for _ in ()).throw(
                                        AgentToolError("AUTH_REQUIRED"))))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture", task="resume_tailor")

        result = runtime.run([{"name": "read_value", "arguments": {"value": "safe"}}])

        self.assertEqual(result["state"], "AUTH_REQUIRED")
        trace = read_agent_trace(self.store, result["run_id"])
        self.assertEqual(trace["terminal"]["stop_reason"], "AUTH_REQUIRED")

    def test_collection_contract_prevents_unknown_or_incomplete_output_from_passing(self):
        from services.agents.acceptance import CollectionContract
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            items: list[dict]
            count: int | None
        registry = ToolRegistry()
        registry.register(AgentTool("collect_jobs", "Collect jobs", ToolClass.READ,
                                    Request, Response,
                                    lambda args, context: {"items": [{"id": "job-1"}],
                                                           "count": None}))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture", task="job_match")

        result = runtime.run([{"name": "collect_jobs", "arguments": {"value": "page-1"}}],
                             collection_contract=CollectionContract(
                                 items_key="items", count_key="count", expected_count=2,
                                 tail_key="id", expected_tail="job-2"))

        self.assertEqual(result["state"], "OUTPUT_UNVERIFIED")
        self.assertEqual(result["stop_reason"], "COUNT_UNKNOWN")
        self.assertEqual(result["collection_result"]["code"], "COUNT_UNKNOWN")
        trace = read_agent_trace(self.store, result["run_id"])
        self.assertEqual(trace["terminal"]["state"], "OUTPUT_UNVERIFIED")
        self.assertEqual(trace["terminal"]["collection_result"]["code"], "COUNT_UNKNOWN")
        self.assertNotIn("job-1", json.dumps(trace))

    def test_collection_contract_records_verified_tail_and_count(self):
        from services.agents.acceptance import CollectionContract
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            items: list[dict]
            count: int
        registry = ToolRegistry()
        registry.register(AgentTool("collect_jobs", "Collect jobs", ToolClass.READ,
                                    Request, Response,
                                    lambda args, context: {"items": [{"id": "job-1"},
                                                                      {"id": "job-2"}],
                                                           "count": 2}))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture", task="job_match")

        result = runtime.run([{"name": "collect_jobs", "arguments": {"value": "page-1"}}],
                             collection_contract=CollectionContract(
                                 items_key="items", count_key="count", expected_count=2,
                                 tail_key="id", expected_tail="job-2"))

        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(result["stop_reason"], "SUCCESS_ARTIFACT_VERIFIED")
        self.assertEqual(result["collection_result"]["observed_count"], 2)
        trace = read_agent_trace(self.store, result["run_id"])
        self.assertEqual(trace["terminal"]["collection_result"]["tail_matches"], True)
        self.assertNotIn("job-2", json.dumps(trace))

    def test_invalid_cost_metadata_does_not_leave_an_unfinished_trace(self):
        before = self.store.count("audit_events")
        with self.assertRaisesRegex(ValueError, "cost metadata"):
            self.runtime().run([], cost_usd=float("nan"))
        self.assertEqual(self.store.count("audit_events"), before)


if __name__ == "__main__":
    unittest.main()
