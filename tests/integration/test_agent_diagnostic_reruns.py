"""Bounded diagnostic reruns are repeatability evidence, not correctness proof."""
from pathlib import Path
import json
import tempfile
import unittest

from pydantic import BaseModel, ConfigDict

from packages.persistence.store import Store
from services.agents.registry import AgentTool, AgentToolError, PolicyDecision, ToolClass, ToolRegistry
from services.agents.runtime import AgentRuntime, read_agent_trace
from tests.integration.test_spine import PROFILE


class Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: str


class Response(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: str


class DiagnosticSliceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        database = Path(self.directory.name) / "diagnostics.db"
        self.url = f"sqlite:///{database.as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def runtime(self, classification, handler, *, policy=None):
        registry = ToolRegistry()
        registry.register(AgentTool("read_value", "Read a synthetic value", classification,
                                    Request, Response, handler, policy))
        return AgentRuntime(self.store, registry, profile_id="fixture", task="resume_tailor")

    def test_diagnostic_slice_is_bounded_and_persists_value_free_attempt_budgets(self):
        results = iter(("observed-a", "observed-b"))
        started = []

        def observe(args, context):
            started.append(args.value)
            context.consume_page()
            return {"value": next(results)}

        runtime = self.runtime(ToolClass.READ, observe)

        result = runtime.diagnose_slice(
            [{"name": "read_value", "arguments": {"value": "synthetic"}}],
            max_attempts=2, max_duration_ms=1_000, max_pages=1)

        self.assertEqual(len(started), 2)
        self.assertEqual(result["attempt_count"], 2)
        self.assertFalse(result["agreement"])
        self.assertEqual(result["correctness"], "NOT_ESTABLISHED")
        self.assertLessEqual(result["total_declared_duration_budget_ms"], 2_000)
        self.assertEqual(result["max_pages_per_attempt"], 1)
        self.assertEqual(len(result["attempts"]), 2)
        for index, attempt in enumerate(result["attempts"], start=1):
            trace = read_agent_trace(self.store, attempt["run_id"])
            self.assertEqual(trace["diagnostic"], {"id": result["diagnostic_id"],
                                                    "attempt": index})
            self.assertEqual(trace["run_limits"]["max_duration_ms"], 1_000)
            self.assertEqual(trace["run_limits"]["max_pages"], 1)
            self.assertEqual(trace["terminal"]["stop_reason"], "COMPLETED")
            self.assertIsInstance(trace["terminal"]["budget_consumed_ms"], int)
            self.assertEqual(trace["terminal"]["diagnostic_attempt"], index)
            self.assertEqual(trace["terminal"]["pages_consumed"], 1)
            self.assertEqual(attempt["pages_consumed"], 1)
            self.assertNotIn("observed-a", json.dumps(trace))
            self.assertNotIn("observed-b", json.dumps(trace))

    def test_terminal_escape_outcomes_stop_further_diagnostic_attempts(self):
        for outcome in ("AUTH_REQUIRED", "NOT_AVAILABLE", "SOURCE_CHANGED"):
            with self.subTest(outcome=outcome):
                invocations = []

                def stop_with_outcome(args, context):
                    invocations.append(args.value)
                    raise AgentToolError(outcome)

                runtime = self.runtime(ToolClass.READ, stop_with_outcome)
                result = runtime.diagnose_slice(
                    [{"name": "read_value", "arguments": {"value": "synthetic"}}],
                    max_attempts=3)

                self.assertEqual(invocations, ["synthetic"])
                self.assertFalse(result["agreement"])
                self.assertEqual(result["attempt_count"], 1)
                self.assertEqual(len(result["attempts"]), 1)
                attempt = result["attempts"][0]
                self.assertEqual(attempt["state"], outcome)
                self.assertEqual(attempt["stop_reason"], outcome)
                trace = read_agent_trace(self.store, attempt["run_id"])
                self.assertEqual(trace["terminal"]["state"], outcome)
                self.assertEqual(trace["terminal"]["stop_reason"], outcome)
                self.assertEqual(trace["steps"][0]["result_code"], outcome)

    def test_matching_diagnostic_attempts_do_not_claim_correctness(self):
        runtime = self.runtime(ToolClass.READ,
                               lambda args, context: {"value": "same synthetic observation"})

        result = runtime.diagnose_slice(
            [{"name": "read_value", "arguments": {"value": "synthetic"}}],
            max_attempts=2)

        self.assertTrue(result["agreement"])
        self.assertEqual(result["correctness"], "NOT_ESTABLISHED")
        self.assertEqual(result["interpretation"], "AGREEMENT_IS_NOT_CORRECTNESS")
        self.assertEqual(result["attempt_count"], 2)

    def test_side_effect_and_oversized_slices_are_rejected_before_any_execution(self):
        executions = []
        runtime = self.runtime(ToolClass.SIDE_EFFECT,
                               lambda args, context: (executions.append(args.value)
                                                      or {"value": args.value}),
                               policy=lambda args, context: PolicyDecision(True, "ALLOWED"))
        before = self.store.count("audit_events")

        with self.assertRaisesRegex(ValueError, "read-only"):
            runtime.diagnose_slice(
                [{"name": "read_value", "arguments": {"value": "synthetic"}}],
                max_attempts=2)
        with self.assertRaisesRegex(ValueError, "three attempts"):
            runtime.diagnose_slice(
                [{"name": "read_value", "arguments": {"value": "synthetic"}}],
                max_attempts=4)
        with self.assertRaisesRegex(ValueError, "two and three attempts"):
            runtime.diagnose_slice(
                [{"name": "read_value", "arguments": {"value": "synthetic"}}],
                max_attempts=1)
        with self.assertRaisesRegex(ValueError, "five tool calls"):
            runtime.diagnose_slice([
                {"name": "read_value", "arguments": {"value": "synthetic"}}
                for _ in range(6)
            ])

        self.assertEqual(executions, [])
        self.assertEqual(self.store.count("audit_events"), before)

    def test_invalid_later_arguments_are_rejected_before_any_attempt(self):
        executions = []
        runtime = self.runtime(
            ToolClass.READ,
            lambda args, context: (executions.append(args.value)
                                   or {"value": args.value}),
        )
        before = self.store.count("audit_events")

        with self.assertRaisesRegex(ValueError, "valid.*arguments"):
            runtime.diagnose_slice([
                {"name": "read_value", "arguments": {"value": "synthetic"}},
                {"name": "read_value", "arguments": {"value": "invalid", "extra": "nope"}},
            ], max_attempts=3)

        self.assertEqual(executions, [])
        self.assertEqual(self.store.count("audit_events"), before)

    def test_aliased_later_call_mutation_cannot_change_a_diagnostic_attempt(self):
        observed = []
        action_executions = []
        mutated = False
        later_call = {"name": "read_value", "arguments": {"value": "target"}}

        def read(args, context):
            nonlocal mutated
            observed.append(args.value)
            if args.value == "target" and not mutated:
                # The handler holds an alias to the caller-owned future call.
                # Mutate it after attempt one's preflight and execution.
                later_call["name"] = "propose_value"
                later_call["arguments"]["value"] = "changed"
                mutated = True
            return {"value": args.value}

        registry = ToolRegistry()
        registry.register(AgentTool("read_value", "Read a synthetic value", ToolClass.READ,
                                    Request, Response, read))
        registry.register(AgentTool(
            "propose_value", "Propose a synthetic value", ToolClass.SIDE_EFFECT,
            Request, Response,
            lambda args, context: (action_executions.append(args.value)
                                   or {"value": args.value}),
            lambda args, context: PolicyDecision(True, "ALLOWED"),
        ))
        runtime = AgentRuntime(self.store, registry, profile_id="fixture",
                               task="resume_tailor")

        result = runtime.diagnose_slice([
            {"name": "read_value", "arguments": {"value": "first"}},
            later_call,
        ], max_attempts=2)

        self.assertEqual(observed, ["first", "target", "first", "target"])
        self.assertEqual(later_call, {
            "name": "propose_value", "arguments": {"value": "changed"},
        })
        self.assertEqual(action_executions, [])
        self.assertEqual(result["attempt_count"], 2)
        for attempt in result["attempts"]:
            trace = read_agent_trace(self.store, attempt["run_id"])
            self.assertEqual([step["tool_name"] for step in trace["steps"]],
                             ["read_value", "read_value"])
            self.assertEqual([step["proposal_id"] for step in trace["steps"]],
                             [None, None])


if __name__ == "__main__":
    unittest.main()
