"""MJ-REF-033: unused calls must not replace the actual durable stop reason."""
from pathlib import Path
import json
import tempfile
import unittest

from pydantic import BaseModel, ConfigDict

from packages.persistence.store import Store
from services.agents.registry import (
    MAX_TOOL_ARGUMENT_BYTES, MAX_TOOL_RESULT_BYTES,
    AgentTool, ToolClass, ToolRegistry,
)
from services.agents.runtime import AgentRuntime
from services.agents.traces import MAX_AGENT_STEPS, read_agent_trace


INPUT_CANARY = "synthetic-private-tool-argument"
ERROR_CANARY = "synthetic-private-handler-result"
PROFILE = {
    "id": "terminal-precedence",
    "name": "Synthetic Agent Profile",
    "email": "terminal@example.invalid",
    "answers": [],
    "facts": [],
}


class ValueInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: str


class ValueOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: str


class AgentTerminalPrecedenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.url = f"sqlite:///{(Path(directory.name) / 'agents.db').as_posix()}"
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.save_profile(PROFILE)
        self.dispatched = []

        def read_value(arguments, _context):
            self.dispatched.append(("read_value", arguments.value))
            return {"value": arguments.value}

        def fail_value(arguments, _context):
            self.dispatched.append(("fail_value", arguments.value))
            raise RuntimeError(ERROR_CANARY)

        def invalid_result(arguments, _context):
            self.dispatched.append(("invalid_result", arguments.value))
            return {"value": 42, "private_detail": ERROR_CANARY}

        def oversized_result(arguments, _context):
            self.dispatched.append(("oversized_result", arguments.value))
            return {"value": ERROR_CANARY + "x" * MAX_TOOL_RESULT_BYTES}

        registry = ToolRegistry()
        for handler in (read_value, fail_value, invalid_result, oversized_result):
            registry.register(AgentTool(
                handler.__name__, "Exercise a synthetic read result", ToolClass.READ,
                ValueInput, ValueOutput, handler,
            ))
        self.runtime = AgentRuntime(
            self.store, registry, profile_id=PROFILE["id"], task="resume_tailor",
        )

    @staticmethod
    def _call(name="read_value", value="safe"):
        return {"name": name, "arguments": {"value": value}}

    def _failure_cases(self):
        return (
            (self._call("unknown_value", INPUT_CANARY),
             "REJECTED", "UNKNOWN_TOOL", False),
            (self._call(value=42), "REJECTED", "INVALID_ARGUMENTS", False),
            (self._call(value=INPUT_CANARY + "x" * MAX_TOOL_ARGUMENT_BYTES),
             "REJECTED", "ARGUMENTS_TOO_LARGE", False),
            (self._call("fail_value", INPUT_CANARY), "FAILED", "TOOL_FAILED", True),
            (self._call("invalid_result", INPUT_CANARY),
             "FAILED", "INVALID_TOOL_RESULT", True),
            (self._call("oversized_result", INPUT_CANARY),
             "FAILED", "RESULT_TOO_LARGE", True),
        )

    def _run_and_reopen(self, calls, expected_codes, expected_dispatch):
        self.dispatched.clear()
        result = self.runtime.run(calls)
        reopened = Store(self.url)
        try:
            trace = read_agent_trace(reopened, result["run_id"])
        finally:
            reopened.engine.dispose()

        self.assertEqual(self.dispatched, expected_dispatch)
        self.assertEqual([step["result_code"] for step in result["steps"]], expected_codes)
        self.assertEqual([step["result_code"] for step in trace["steps"]], expected_codes)
        self.assertEqual(trace["terminal"]["step_count"], len(expected_codes))
        self.assertEqual(trace["run_limits"]["step_limit"], MAX_AGENT_STEPS)
        self.assertIsInstance(trace["terminal"]["budget_consumed_ms"], int)
        self.assertGreaterEqual(trace["terminal"]["budget_consumed_ms"], 0)
        self.assertEqual(trace["terminal"]["pages_consumed"], 0)
        self.assertIsNone(trace["terminal"]["collection_result"])
        self.assertNotIn(ERROR_CANARY, json.dumps(result))
        serialized_trace = json.dumps(trace)
        for private_value in (INPUT_CANARY, ERROR_CANARY, PROFILE["email"]):
            self.assertNotIn(private_value, serialized_trace)
        for step in trace["steps"]:
            self.assertNotIn("arguments", step)
            self.assertNotIn("output", step)
        return result, trace

    def _assert_terminal(self, result, trace, state, reason):
        self.assertEqual(
            (result["state"], result["stop_reason"],
             trace["terminal"]["state"], trace["terminal"]["stop_reason"]),
            (state, reason, state, reason),
        )

    def _assert_failure_with_unused_tail(self, successful_prefix):
        prefix = [self._call(value=f"prefix-{index}")
                  for index in range(successful_prefix)]
        prefix_dispatch = [("read_value", f"prefix-{index}")
                           for index in range(successful_prefix)]
        tail = [self._call(value=f"must-not-run-{index}")
                for index in range(MAX_AGENT_STEPS - successful_prefix)]

        for failed_call, state, code, handler_entered in self._failure_cases():
            with self.subTest(prefix=successful_prefix, failure=code):
                expected_dispatch = prefix_dispatch + (
                    [(failed_call["name"], failed_call["arguments"]["value"])]
                    if handler_entered else []
                )
                expected_codes = ["OK"] * successful_prefix + [code]
                short, short_trace = self._run_and_reopen(
                    prefix + [failed_call], expected_codes, expected_dispatch,
                )
                # Preserve the established error contract for the same executed
                # prefix, rather than inventing a different stop-reason API.
                self._assert_terminal(short, short_trace, state, state)

                oversized, trace = self._run_and_reopen(
                    prefix + [failed_call] + tail, expected_codes, expected_dispatch,
                )
                self.assertEqual(len(prefix + [failed_call] + tail), MAX_AGENT_STEPS + 1)
                self.assertEqual(oversized["steps"][-1]["output"], {})
                self.assertIsNone(oversized["steps"][-1]["proposal_id"])
                self._assert_terminal(oversized, trace, state, short["stop_reason"])

    def test_first_failure_survives_an_oversized_requested_batch(self):
        self._assert_failure_with_unused_tail(successful_prefix=0)

    def test_failure_on_last_permitted_call_wins_over_unused_tail(self):
        self._assert_failure_with_unused_tail(successful_prefix=MAX_AGENT_STEPS - 1)

    def test_only_genuine_truncation_reports_step_exhaustion(self):
        for requested in (MAX_AGENT_STEPS - 1, MAX_AGENT_STEPS, MAX_AGENT_STEPS + 1):
            with self.subTest(requested=requested):
                executed = min(requested, MAX_AGENT_STEPS)
                result, trace = self._run_and_reopen(
                    [self._call(value=f"success-{index}") for index in range(requested)],
                    ["OK"] * executed,
                    [("read_value", f"success-{index}") for index in range(executed)],
                )
                if requested > MAX_AGENT_STEPS:
                    self._assert_terminal(result, trace, "LIMIT_REACHED", "STEP_BUDGET_EXHAUSTED")
                else:
                    self._assert_terminal(result, trace, "COMPLETED", "COMPLETED")
