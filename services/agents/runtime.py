"""Bounded local agent runs and human-reviewed proposal execution."""
from __future__ import annotations

from hashlib import sha256
import re
from typing import Any

from services.career_profile.profile_access import TASK_PATHS
from services.agents.registry import (
    AgentToolContext, AgentToolError, PolicyDecision, ReviewDecision,
    ToolClass, ToolRegistry,
)
from services.agents.traces import (
    MAX_AGENT_STEPS, append_agent_event, new_run_id, read_agent_trace,
)


_SAFE_TOOL = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class AgentRuntime:
    """Runs only registered JSON tools against a host-bound profile/task scope."""

    def __init__(self, store, registry: ToolRegistry, *, profile_id: str, task: str):
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("Agent runtime needs a bound profile")
        if not isinstance(task, str) or task not in TASK_PATHS:
            raise ValueError("Agent runtime task is not approved")
        store.profile(profile_id)
        self.store = store
        self.registry = registry
        self.profile_id = profile_id
        self.task = task
        self.registry.seal()

    def run(self, calls: object, *, cost_usd: float | None = None,
            input_tokens: int | None = None, output_tokens: int | None = None) -> dict[str, Any]:
        self._validate_metrics(cost_usd, input_tokens, output_tokens)
        run_id = new_run_id()
        profile_ref = sha256(self.profile_id.encode("utf-8")).hexdigest()[:24]
        append_agent_event(self.store, run_id, "AGENT_RUN_STARTED", {
            "schema_version": 1, "task": self.task, "profile_ref": profile_ref,
            "step_limit": MAX_AGENT_STEPS,
        })
        if not isinstance(calls, list):
            calls = []
            state = "REJECTED"
        else:
            state = "COMPLETED"
        results = []
        context = AgentToolContext(run_id=run_id, profile_id=self.profile_id,
                                   task=self.task)
        for index, call in enumerate(calls[:MAX_AGENT_STEPS], start=1):
            name = call.get("name") if isinstance(call, dict) else None
            safe_name = name if isinstance(name, str) and _SAFE_TOOL.fullmatch(name) else "INVALID_TOOL_NAME"
            arguments = call.get("arguments") if isinstance(call, dict) else None
            try:
                output, proposal_id = self.registry.invoke(name, arguments, context)
                result_code = "PROPOSAL_PENDING" if proposal_id else "OK"
            except AgentToolError as error:
                output, proposal_id, result_code = {}, None, error.code
                state = "REJECTED" if error.code in ("UNKNOWN_TOOL", "INVALID_ARGUMENTS", "ARGUMENTS_TOO_LARGE") else "FAILED"
            append_agent_event(self.store, run_id, "AGENT_TOOL_STEP", {
                "step": index, "tool_name": safe_name,
                "classification": self._classification(name),
                "result_code": result_code,
                "proposal_id": proposal_id,
                "cost_usd": None,
            })
            results.append({"tool_name": safe_name, "result_code": result_code,
                            "output": output, "proposal_id": proposal_id})
            if state in ("REJECTED", "FAILED"):
                break
            if proposal_id:
                state = "REVIEW_REQUIRED"
        if len(calls) > MAX_AGENT_STEPS:
            state = "LIMIT_REACHED"
        append_agent_event(self.store, run_id, "AGENT_RUN_TERMINAL", {
            "state": state, "step_count": len(results), "cost_usd": cost_usd,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
        })
        return {"run_id": run_id, "state": state, "steps": results}

    @staticmethod
    def _validate_metrics(cost_usd, input_tokens, output_tokens):
        if cost_usd is not None and (type(cost_usd) not in (int, float)
                                     or not 0 <= cost_usd <= 100):
            raise ValueError("Agent cost metadata must be between zero and one hundred dollars")
        for value in (input_tokens, output_tokens):
            if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000):
                raise ValueError("Agent token metadata is outside bounds")

    def _classification(self, name: object) -> str:
        try:
            return self.registry.get(name).classification.value
        except AgentToolError:
            return "UNREGISTERED"

    def review(self, run_id: str, proposal_id: str, tool_name: str,
               arguments: object, decision: ReviewDecision) -> dict[str, str]:
        if not isinstance(decision, ReviewDecision):
            raise ValueError("An explicit review decision is required")
        trace = read_agent_trace(self.store, run_id)
        if trace.get("terminal", {}).get("state") != "REVIEW_REQUIRED":
            raise ValueError("Agent run has no pending side-effect review")
        proposal = next((step for step in trace["steps"]
                         if step.get("proposal_id") == proposal_id
                         and step.get("tool_name") == tool_name), None)
        if proposal is None:
            raise ValueError("Proposal arguments do not match the recorded proposal")
        context = AgentToolContext(run_id=run_id, profile_id=self.profile_id,
                                   task=self.task, proposal_id=proposal_id)
        if self.registry.proposal_id(tool_name, arguments, context) != proposal_id:
            raise ValueError("Proposal arguments do not match the recorded proposal")
        self._reserve_review(run_id, proposal_id, tool_name, decision)
        if not decision.approved:
            append_agent_event(self.store, run_id, "AGENT_PROPOSAL_REJECTED", {
                "proposal_id": proposal_id, "tool_name": tool_name,
                "reviewer_ref": sha256(decision.reviewer_id.encode("utf-8")).hexdigest()[:24],
                "result_code": "USER_REJECTED",
            })
            return {"result_code": "USER_REJECTED"}

        tool = self.registry.get(tool_name)
        parsed, _ = self.registry.parse(tool, arguments)
        policy = tool.policy(parsed, context)
        if not isinstance(policy, PolicyDecision) or type(policy.allowed) is not bool:
            append_agent_event(self.store, run_id, "AGENT_PROPOSAL_REJECTED", {
                "proposal_id": proposal_id, "tool_name": tool_name,
                "result_code": "INVALID_POLICY_DECISION",
            })
            return {"result_code": "INVALID_POLICY_DECISION"}
        if not policy.allowed:
            append_agent_event(self.store, run_id, "AGENT_PROPOSAL_REJECTED", {
                "proposal_id": proposal_id, "tool_name": tool_name,
                "result_code": "POLICY_DENIED",
            })
            return {"result_code": "POLICY_DENIED"}
        append_agent_event(self.store, run_id, "AGENT_PROPOSAL_EXECUTION_STARTED", {
            "proposal_id": proposal_id, "tool_name": tool_name,
            "policy_code": policy.result_code,
        })
        try:
            self.registry.execute_side_effect(tool_name, arguments, context)
        except AgentToolError as error:
            append_agent_event(self.store, run_id, "AGENT_PROPOSAL_REJECTED", {
                "proposal_id": proposal_id, "tool_name": tool_name,
                "result_code": error.code,
            })
            return {"result_code": error.code}
        append_agent_event(self.store, run_id, "AGENT_PROPOSAL_EXECUTED", {
            "proposal_id": proposal_id, "tool_name": tool_name,
            "result_code": "EXECUTED",
        })
        return {"result_code": "EXECUTED"}

    def _reserve_review(self, run_id: str, proposal_id: str, tool_name: str,
                        decision: ReviewDecision) -> None:
        # Refuse replays and concurrent approvals before calling a side-effect handler.
        from sqlalchemy import select, text
        from packages.persistence import models as m
        from packages.persistence.store import now

        with self.store.engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                lock_key = int.from_bytes(bytes.fromhex(proposal_id[:16]), "big") & 0x7FFFFFFFFFFFFFFF
                connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            entity = "agent-run:" + run_id
            prior = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == entity,
                m.audit.c.action.in_(("AGENT_PROPOSAL_REVIEWED",
                                      "AGENT_PROPOSAL_EXECUTION_STARTED",
                                      "AGENT_PROPOSAL_EXECUTED",
                                      "AGENT_PROPOSAL_REJECTED")),
            )).mappings())
            if any(row["payload"].get("proposal_id") == proposal_id for row in prior):
                raise ValueError("Proposal was already reviewed or reserved")
            self.store._audit(connection, entity, "AGENT_PROPOSAL_REVIEWED", {
                "proposal_id": proposal_id, "tool_name": tool_name,
                "reviewer_ref": sha256(decision.reviewer_id.encode("utf-8")).hexdigest()[:24],
                "result_code": "USER_APPROVED",
                "reviewed_at": now(),
            })


__all__ = ["AgentRuntime", "ReviewDecision", "read_agent_trace"]
