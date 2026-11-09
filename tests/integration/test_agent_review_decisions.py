"""Human review decisions remain truthful and single-use after reopening storage."""
from pathlib import Path
import json
import tempfile
from typing import Literal
import unittest

from pydantic import BaseModel, ConfigDict

from packages.persistence.store import Store
from services.agents.registry import (
    AgentTool, PolicyDecision, ReviewDecision, ToolClass, ToolRegistry,
)
from services.agents.runtime import AgentRuntime, read_agent_trace


class _Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    value: str


class _Proposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    proposal_id: str
    tool_name: str
    classification: Literal["SIDE_EFFECT"]
    arguments: dict
    review_required: Literal[True]


class AgentReviewDecisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-review-decision-")
        database = Path(self.directory.name) / "reviews.db"
        self.url = f"sqlite:///{database.as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.save_profile({
            "id": "review-fixture", "name": "Review Fixture",
            "email": "review@example.invalid", "facts": [], "answers": [],
        })
        self.executions = []
        self.arguments = {"value": "private-review-value-canary"}
        self.reviewer = "private-reviewer-canary"

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def runtime(self, *, policy_allowed=True):
        def execute(args, context):
            self.executions.append(args.value)
            return {"value": args.value}

        registry = ToolRegistry()
        registry.register(AgentTool(
            name="set_fixture_value", description="Review a synthetic local value",
            classification=ToolClass.SIDE_EFFECT, input_model=_Request,
            output_model=_Proposal, handler=execute,
            policy=lambda args, context: PolicyDecision(policy_allowed, "FIXTURE_POLICY"),
            execution_model=_Request,
        ))
        return AgentRuntime(self.store, registry, profile_id="review-fixture", task="job_match")

    def propose(self, runtime):
        result = runtime.run([{"name": "set_fixture_value", "arguments": self.arguments}])
        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        self.assertEqual(self.executions, [])
        return result["run_id"], result["steps"][0]["proposal_id"]

    def review(self, runtime, identifiers, approved):
        return runtime.review(
            *identifiers, "set_fixture_value", self.arguments,
            ReviewDecision(reviewer_id=self.reviewer, approved=approved))

    def reopen_trace(self, run_id):
        self.store.engine.dispose()
        self.store = Store(self.url)
        trace = read_agent_trace(self.store, run_id)
        serialized = json.dumps(trace)
        self.assertNotIn(self.arguments["value"], serialized)
        self.assertNotIn(self.reviewer, serialized)
        self.assertNotIn("review@example.invalid", serialized)
        return trace

    def test_rejection_is_durable_and_cannot_be_replayed_or_changed_to_approval(self):
        runtime = self.runtime()
        identifiers = self.propose(runtime)
        self.assertEqual(self.review(runtime, identifiers, False), {"result_code": "USER_REJECTED"})

        trace = self.reopen_trace(identifiers[0])
        self.assertEqual(
            [(event["action"], event.get("result_code")) for event in trace["proposal_events"]],
            [("AGENT_PROPOSAL_REVIEWED", "USER_REJECTED"),
             ("AGENT_PROPOSAL_REJECTED", "USER_REJECTED")],
        )
        self.assertNotIn("USER_APPROVED", json.dumps(trace))
        resumed = self.runtime()
        for approved in (False, True):
            with self.subTest(approved=approved), self.assertRaisesRegex(ValueError, "already reviewed"):
                self.review(resumed, identifiers, approved)
        self.assertEqual(read_agent_trace(self.store, identifiers[0]), trace)
        self.assertEqual(self.executions, [])

    def test_approval_is_durable_and_executes_only_once(self):
        runtime = self.runtime()
        identifiers = self.propose(runtime)
        self.assertEqual(self.review(runtime, identifiers, True), {"result_code": "EXECUTED"})

        trace = self.reopen_trace(identifiers[0])
        self.assertEqual(
            [(event["action"], event.get("result_code")) for event in trace["proposal_events"]],
            [("AGENT_PROPOSAL_REVIEWED", "USER_APPROVED"),
             ("AGENT_PROPOSAL_EXECUTION_STARTED", None),
             ("AGENT_PROPOSAL_EXECUTED", "EXECUTED")],
        )
        resumed = self.runtime()
        for approved in (True, False):
            with self.subTest(approved=approved), self.assertRaisesRegex(ValueError, "already reviewed"):
                self.review(resumed, identifiers, approved)
        self.assertEqual(read_agent_trace(self.store, identifiers[0]), trace)
        self.assertEqual(self.executions, [self.arguments["value"]])

    def test_policy_denial_keeps_the_actual_human_approval_in_the_audit(self):
        runtime = self.runtime(policy_allowed=False)
        identifiers = self.propose(runtime)
        self.assertEqual(self.review(runtime, identifiers, True), {"result_code": "POLICY_DENIED"})

        trace = self.reopen_trace(identifiers[0])
        self.assertEqual(
            [(event["action"], event.get("result_code")) for event in trace["proposal_events"]],
            [("AGENT_PROPOSAL_REVIEWED", "USER_APPROVED"),
             ("AGENT_PROPOSAL_REJECTED", "POLICY_DENIED")],
        )
        with self.assertRaisesRegex(ValueError, "already reviewed"):
            self.review(self.runtime(), identifiers, True)
        self.assertEqual(read_agent_trace(self.store, identifiers[0]), trace)
        self.assertEqual(self.executions, [])


if __name__ == "__main__":
    unittest.main()
