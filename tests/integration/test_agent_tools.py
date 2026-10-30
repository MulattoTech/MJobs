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

    def test_page_budget_blocks_the_next_fetch_and_persists_only_consumption(self):
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            pages: list[str]

        started_pages = []

        def collect_pages(args, context):
            pages = []
            for page in range(1, 4):
                context.consume_page()
                started_pages.append(page)
                pages.append(f"private-page-{page}")
            return {"pages": pages}

        registry = ToolRegistry()
        registry.register(AgentTool("collect_pages", "Collect pages", ToolClass.READ,
                                    Request, Response, collect_pages))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture", task="job_match")

        result = runtime.run([{"name": "collect_pages", "arguments": {"value": "jobs"}}],
                             max_pages=2)

        self.assertEqual(started_pages, [1, 2])
        self.assertEqual(result["state"], "LIMIT_REACHED")
        self.assertEqual(result["stop_reason"], "PAGE_BUDGET_EXHAUSTED")
        trace = read_agent_trace(self.store, result["run_id"])
        self.assertEqual(trace["terminal"]["pages_consumed"], 2)
        self.assertEqual(trace["terminal"]["max_pages"], 2)
        self.assertNotIn("private-page", json.dumps(trace))

    def test_page_consuming_tool_requires_an_explicit_budget(self):
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str

        starts = []

        def fetch_page(args, context):
            context.consume_page()
            starts.append("fetch")
            return {"value": "private-page"}

        registry = ToolRegistry()
        registry.register(AgentTool("fetch_page", "Fetch one page", ToolClass.READ,
                                    Request, Response, fetch_page))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture", task="job_match")

        result = runtime.run([{"name": "fetch_page", "arguments": {"value": "jobs"}}])

        self.assertEqual(starts, [])
        self.assertEqual(result["state"], "LIMIT_REACHED")
        self.assertEqual(result["stop_reason"], "PAGE_BUDGET_REQUIRED")

    def test_explicit_escape_outcomes_are_terminal_results(self):
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        for outcome in ("AUTH_REQUIRED", "NOT_AVAILABLE", "SOURCE_CHANGED"):
            with self.subTest(outcome=outcome):
                registry = ToolRegistry()
                registry.register(AgentTool("read_value", "Read value", ToolClass.READ,
                                            Request, Response,
                                            lambda args, context: (_ for _ in ()).throw(
                                                AgentToolError(outcome))))
                runtime = AgentRuntime(self.store, registry, profile_id="fixture",
                                       task="resume_tailor")

                result = runtime.run([{"name": "read_value", "arguments": {"value": "safe"}}])

                self.assertEqual(result["state"], outcome)
                trace = read_agent_trace(self.store, result["run_id"])
                self.assertEqual(trace["terminal"]["stop_reason"], outcome)

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

    def slice_runtime(self, handler=None):
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str

        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str

        seen = []

        def observe(args, context):
            seen.append(args.value)
            return handler(args, context) if handler else {"value": args.value}

        registry = ToolRegistry()
        for name, classification in (("read_value", ToolClass.READ),
                                     ("propose_value", ToolClass.PROPOSE)):
            registry.register(AgentTool(name, "Synthetic observation", classification,
                                        Request, Response, observe))
        return AgentRuntime(self.store, registry, profile_id="fixture", task="resume_tailor"), seen

    @staticmethod
    def slice_calls(*values):
        return [{"name": "read_value", "arguments": {"value": value}} for value in values]

    def test_prior_slice_selects_snapshot_and_persists_only_diagnostic_linkage(self):
        runtime, seen = self.slice_runtime()
        calls = self.slice_calls("excluded-canary", "private-argument-canary", "tail-canary")
        prior = runtime.run(calls, max_duration_ms=900, max_pages=2)
        calls[1]["arguments"]["value"] = "mutated-canary"
        seen.clear()
        first = runtime.rerun_prior_slice(prior["run_id"], 1, 2)
        second = runtime.rerun_prior_slice(prior["run_id"], 1, 2)
        self.assertEqual(seen, ["private-argument-canary"] * 2)
        self.assertEqual(first["steps"], second["steps"])
        self.assertEqual(len({prior["run_id"], first["run_id"], second["run_id"]}), 3)
        reopened = Store(self.url)
        try:
            for result in (first, second):
                self.assertEqual(result["state"], "DIAGNOSTIC_ONLY")
                self.assertEqual(result["stop_reason"], "DIAGNOSTIC_ONLY")
                self.assertIsNone(result["collection_result"])
                trace = read_agent_trace(reopened, result["run_id"])
                self.assertEqual(trace["run_limits"], {
                    "step_limit": 1, "max_duration_ms": 900, "max_pages": 2,
                    "success_artifact": "DIAGNOSTIC_ONLY"})
                terminal = trace["terminal"]
                self.assertEqual(terminal["state"], "OUTPUT_UNVERIFIED")
                self.assertEqual(terminal["stop_reason"], "DIAGNOSTIC_ONLY")
                self.assertEqual(terminal["prior_run_id"], prior["run_id"])
                self.assertEqual((terminal["slice_start"], terminal["slice_stop"]), (1, 2))
                self.assertNotIn("COMPLETED", json.dumps(trace))
                self.assertNotIn("SUCCESS_ARTIFACT_VERIFIED", json.dumps(trace))
            with reopened.engine.connect() as connection:
                payloads = [row["payload"] for row in connection.execute(m.audit.select()).mappings()]
            self.assertNotIn("canary", json.dumps(payloads))
        finally:
            reopened.engine.dispose()

    def test_prior_slice_rejects_bad_bounds_and_limits_before_any_dispatch(self):
        runtime, seen = self.slice_runtime()
        prior = runtime.run(self.slice_calls(*[str(i) for i in range(6)]))
        before = self.store.count("audit_events")
        seen.clear()
        for start, stop in ((0, 0), (2, 1), (-1, 2), (0, 7), (0, 6),
                            (True, 2), (0, False), (0.0, 1), (0, "2"), (None, 2)):
            with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                runtime.rerun_prior_slice(prior["run_id"], start, stop)
        self.assertEqual(seen, [])
        self.assertEqual(self.store.count("audit_events"), before)
        result = runtime.rerun_prior_slice(prior["run_id"], 1, 6)
        self.assertEqual(seen, ["1", "2", "3", "4", "5"])
        self.assertEqual(len(result["steps"]), 5)

    def test_prior_slice_rejects_foreign_stale_and_malformed_bindings(self):
        from services.agents import runtime as runtime_module
        runtime, seen = self.slice_runtime()
        with patch.object(runtime_module, "MAX_PRIOR_RUN_BINDINGS", 2):
            prior = runtime.run(self.slice_calls("first"))
            foreign = AgentRuntime(self.store, runtime.registry,
                                   profile_id="fixture", task="resume_tailor")
            with self.assertRaises(ValueError):
                foreign.rerun_prior_slice(prior["run_id"], 0, 1)
            for attribute, value in (("profile_id", "other"), ("task", "job_match")):
                with patch.object(runtime, attribute, value), self.assertRaises(ValueError):
                    runtime.rerun_prior_slice(prior["run_id"], 0, 1)
            runtime.run(self.slice_calls("second"))
            current = runtime.run(self.slice_calls("third"))
            before = self.store.count("audit_events")
            seen.clear()
            for run_id in (None, [], {}, True, "private-id-canary", "0" * 32, prior["run_id"]):
                with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                    runtime.rerun_prior_slice(run_id, 0, 1)
            self.assertEqual(seen, [])
            self.assertEqual(self.store.count("audit_events"), before)
            self.assertEqual(runtime.rerun_prior_slice(current["run_id"], 0, 1)["state"],
                             "DIAGNOSTIC_ONLY")

    def test_prior_slice_prevalidates_entire_slice_and_never_replays_proposals(self):
        runtime, seen = self.slice_runtime()
        calls = self.slice_calls("read-canary", "proposal-canary")
        calls[1]["name"] = "propose_value"
        prior = runtime.run(calls)
        seen.clear()
        before = self.store.count("audit_events")
        with self.assertRaises(ValueError):
            runtime.rerun_prior_slice(prior["run_id"], 0, 2)
        self.assertEqual(seen, [])
        self.assertEqual(self.store.count("audit_events"), before)
        side_effect_runtime = self.runtime("job_match")
        pending = side_effect_runtime.run([{
            "name": "set_job_disposition", "arguments": {
                "job_id": "job-one", "disposition": "SAVED"}}])
        before = self.store.count("audit_events")
        with self.assertRaises(ValueError):
            side_effect_runtime.rerun_prior_slice(pending["run_id"], 0, 1)
        self.assertEqual(self.store.count("audit_events"), before)
        self.assertEqual(read_job_tracking(self.store, "fixture", "job-one")["disposition"], "ACTIVE")

    def test_prior_slice_inherits_time_budget_and_stops_before_next_handler(self):
        from services.agents import runtime as runtime_module
        clock = [10.0]
        slow = [False]

        def handler(args, context):
            context.check_budget()
            if slow[0]:
                clock[0] += 0.6
            return {"value": args.value}

        runtime, seen = self.slice_runtime(handler)
        with patch.object(runtime_module.time, "monotonic", lambda: clock[0]):
            prior = runtime.run(self.slice_calls("first", "second"), max_duration_ms=500)
            seen.clear()
            slow[0] = True
            result = runtime.rerun_prior_slice(prior["run_id"], 0, 2)
        self.assertEqual(seen, ["first"])
        self.assertEqual(result["state"], "DIAGNOSTIC_ONLY")
        terminal = read_agent_trace(self.store, result["run_id"])["terminal"]
        self.assertEqual(terminal["diagnostic_outcome"], "TIME_BUDGET_EXHAUSTED")
        self.assertEqual(terminal["budget_consumed_ms"], 600)

    def test_prior_slice_rejects_invalid_call_and_stale_tool_without_dispatch(self):
        from dataclasses import replace
        runtime, seen = self.slice_runtime()
        calls = self.slice_calls("valid", "invalid")
        calls[1]["arguments"]["value"] = []
        invalid = runtime.run(calls)
        valid = runtime.run(self.slice_calls("valid"))
        seen.clear()
        before = self.store.count("audit_events")
        with self.assertRaises(ValueError):
            runtime.rerun_prior_slice(invalid["run_id"], 0, 2)
        original = runtime.registry.get("read_value")
        replacement = replace(original, handler=lambda args, context: seen.append("unexpected"))
        with patch.dict(runtime.registry._tools, {"read_value": replacement}):
            with self.assertRaises(ValueError):
                runtime.rerun_prior_slice(valid["run_id"], 0, 1)
        self.assertEqual(seen, [])
        self.assertEqual(self.store.count("audit_events"), before)

    def test_prior_slice_inherits_page_budget_and_keeps_budget_failure_diagnostic(self):
        pages_per_call = [1]
        fetched = []

        def handler(args, context):
            for _ in range(pages_per_call[0]):
                context.consume_page()
                fetched.append(args.value)
            return {"value": args.value}

        runtime, seen = self.slice_runtime(handler)
        prior = runtime.run(self.slice_calls("first", "second"), max_pages=2)
        fetched.clear()
        seen.clear()
        pages_per_call[0] = 3
        result = runtime.rerun_prior_slice(prior["run_id"], 0, 2)
        self.assertEqual(seen, ["first"])
        self.assertEqual(fetched, ["first", "first"])
        self.assertEqual(result["state"], "DIAGNOSTIC_ONLY")
        terminal = read_agent_trace(self.store, result["run_id"])["terminal"]
        self.assertEqual(terminal["diagnostic_outcome"], "PAGE_BUDGET_EXHAUSTED")
        self.assertEqual(terminal["pages_consumed"], 2)
        self.assertEqual(terminal["stop_reason"], "DIAGNOSTIC_ONLY")

    def test_prior_slice_cannot_replay_unexecuted_calls_or_promote_verified_collection(self):
        from services.agents.acceptance import CollectionContract
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            value: str
        class Response(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid")
            items: list[dict]
        registry = ToolRegistry()
        registry.register(AgentTool("read_items", "Read synthetic items", ToolClass.READ,
                                    Request, Response, lambda args, context: {
                                        "items": [{"id": args.value}]}))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture", task="resume_tailor")
        call = {"name": "read_items", "arguments": {"value": "private-output-canary"}}
        prior = runtime.run([call], collection_contract=CollectionContract(
            items_key="items", expected_count=1))
        self.assertEqual(prior["stop_reason"], "SUCCESS_ARTIFACT_VERIFIED")
        for _ in range(2):
            result = runtime.rerun_prior_slice(prior["run_id"], 0, 1)
            self.assertEqual(result["state"], "DIAGNOSTIC_ONLY")
            trace = read_agent_trace(self.store, result["run_id"])
            self.assertIsNone(trace["terminal"]["collection_result"])
            self.assertNotIn("SUCCESS_ARTIFACT_VERIFIED", json.dumps(trace))
            self.assertNotIn("private-output-canary", json.dumps(trace))
        truncated = runtime.run([call] * 21)
        with self.assertRaises(ValueError):
            runtime.rerun_prior_slice(truncated["run_id"], 19, 21)


if __name__ == "__main__":
    unittest.main()
