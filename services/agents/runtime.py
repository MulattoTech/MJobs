"""Bounded local agent runs and human-reviewed proposal execution."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
import time
from typing import Any

from services.career_profile.profile_access import TASK_PATHS
from services.agents.registry import (
    AgentTool, AgentToolContext, AgentToolError, PageBudget, PolicyDecision, ReviewDecision,
    ToolClass, ToolRegistry,
)
from services.agents.acceptance import CollectionContract, validate_collection_output
from services.agents.traces import (
    MAX_AGENT_STEPS, MAX_DIAGNOSTIC_ATTEMPTS, append_agent_event, new_run_id,
    read_agent_trace,
)


_SAFE_TOOL = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
DEFAULT_RUN_DURATION_MS = 30_000
MAX_RUN_DURATION_MS = 120_000
MAX_RUN_PAGES = 100
MAX_DIAGNOSTIC_SLICE_STEPS = 5
MAX_PRIOR_RUN_BINDINGS = 16
_ESCAPE_OUTCOMES = {"AUTH_REQUIRED", "NOT_AVAILABLE", "SOURCE_CHANGED"}


@dataclass(frozen=True)
class _PriorRunBinding:
    scope: tuple[object, ...]
    calls: tuple[tuple[AgentTool, str] | None, ...]
    max_duration_ms: int
    max_pages: int | None


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
        # Bounded, process-local input snapshots only. Never persist arguments
        # or outputs, and never reconstruct a binding from a trace/run ID.
        self._prior_runs: dict[str, _PriorRunBinding] = {}

    def rerun_prior_slice(self, prior_run_id: str, start: int, stop: int) -> dict[str, Any]:
        """Reobserve executed calls [start, stop) from this runtime only.

        Bindings expire after sixteen later source runs or runtime disposal.
        A rerun has a fresh deadline and page permits with the source maxima,
        and at most five steps. Like ordinary runs, deadline enforcement is
        cooperative at handler boundaries and via AgentToolContext; a blocked
        synchronous handler is not preempted. Agreement is never acceptance.
        """
        if (not isinstance(prior_run_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", prior_run_id)):
            raise ValueError("Prior run binding is unavailable")
        binding = self._prior_runs.get(prior_run_id)
        if (binding is None
                or binding.scope != (self.store, self.registry, self.profile_id, self.task)):
            raise ValueError("Prior run binding is unavailable")
        if (type(start) is not int or type(stop) is not int
                or not 0 <= start < stop <= len(binding.calls)
                or stop - start > MAX_DIAGNOSTIC_SLICE_STEPS):
            raise ValueError("Prior slice must select one to five executed calls")
        calls = []
        # Prevalidate the whole slice before creating a trace or invoking any
        # handler. Non-READ and invalid calls have no replayable snapshot.
        for snapshot in binding.calls[start:stop]:
            if snapshot is None:
                raise ValueError("Prior slices require registered read-only calls")
            tool, arguments = snapshot
            try:
                current = self.registry.get(tool.name)
                if current is not tool or current.classification != ToolClass.READ:
                    raise ValueError("Prior read-only tool binding is stale")
                self.registry.parse(current, json.loads(arguments))
            except AgentToolError:
                raise ValueError("Prior read-only tool binding is stale") from None
            calls.append({"name": tool.name, "arguments": json.loads(arguments)})
        return self._run(calls, max_duration_ms=binding.max_duration_ms,
                         max_pages=binding.max_pages,
                         _prior_slice=(prior_run_id, start, stop))

    def _read_snapshot(self, name: object, arguments: object) -> tuple[AgentTool, str] | None:
        try:
            tool = self.registry.get(name)
            if tool.classification == ToolClass.READ:
                _, canonical = self.registry.parse(tool, arguments)
                return tool, canonical
        except AgentToolError:
            pass
        return None

    def diagnose_slice(self, calls: object, *, max_attempts: int = 2,
                       max_duration_ms: int = DEFAULT_RUN_DURATION_MS,
                       max_pages: int | None = None,
                       collection_contract: CollectionContract | None = None) -> dict[str, Any]:
        """Repeat a small read-only slice; agreement is diagnostic, never correctness proof.

        Each attempt is a separately persisted run with its own duration/page
        budgets. Attempt count and calls per slice are hard-capped. Duration
        checks remain cooperative between tool calls; this does not preempt a
        synchronous handler already in progress.
        """
        if type(max_attempts) is not int or not 2 <= max_attempts <= MAX_DIAGNOSTIC_ATTEMPTS:
            raise ValueError("Diagnostic reruns must be between two and three attempts")
        if (not isinstance(calls, list) or not calls
                or len(calls) > MAX_DIAGNOSTIC_SLICE_STEPS):
            raise ValueError("A diagnostic slice must contain one to five tool calls")
        if (type(max_duration_ms) is not int
                or not 1 <= max_duration_ms <= MAX_RUN_DURATION_MS):
            raise ValueError("Agent run duration must be between one and 120000 milliseconds")
        if max_pages is not None and (type(max_pages) is not int
                                      or not 1 <= max_pages <= MAX_RUN_PAGES):
            raise ValueError("Agent page budget must be between one and 100 pages")
        if (collection_contract is not None
                and not isinstance(collection_contract, CollectionContract)):
            raise ValueError("Agent collection success contract is invalid")

        # Snapshot the complete, validated slice before any attempt. Store
        # arguments as canonical JSON text so a read handler holding an alias
        # to the caller's input cannot change later-attempt dispatch or data.
        # Replaying a proposal or side effect, even once, is not a diagnostic
        # read.
        validated_calls = []
        for call in calls:
            if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
                raise ValueError("Diagnostic slices require registered read-only tool calls")
            try:
                tool = self.registry.get(call["name"])
            except AgentToolError:
                raise ValueError("Diagnostic slices require registered read-only tool calls") from None
            if tool.classification != ToolClass.READ:
                raise ValueError("Diagnostic slices support read-only tools only")
            try:
                _, canonical_arguments = self.registry.parse(tool, call["arguments"])
            except AgentToolError:
                raise ValueError(
                    "Diagnostic slices require valid registered read-only tool arguments"
                ) from None
            validated_calls.append((tool.name, canonical_arguments))
        validated_calls = tuple(validated_calls)

        diagnostic_id = new_run_id()
        attempts = []
        signatures = []
        for attempt_number in range(1, max_attempts + 1):
            run_result = self.run(
                [{"name": name, "arguments": json.loads(arguments)}
                 for name, arguments in validated_calls],
                max_duration_ms=max_duration_ms, max_pages=max_pages,
                collection_contract=collection_contract,
                _diagnostic_id=diagnostic_id, _diagnostic_attempt=attempt_number,
            )
            trace = read_agent_trace(self.store, run_result["run_id"])
            terminal = trace["terminal"]
            attempts.append({
                "run_id": run_result["run_id"],
                "state": run_result["state"],
                "stop_reason": run_result["stop_reason"],
                "step_count": len(run_result["steps"]),
                "budget_consumed_ms": terminal["budget_consumed_ms"],
                "pages_consumed": run_result["pages_consumed"],
            })
            signatures.append((
                run_result["state"], run_result["stop_reason"],
                tuple((step["tool_name"], step["result_code"], step["output"])
                      for step in run_result["steps"]),
                run_result["pages_consumed"], run_result["collection_result"],
            ))
            if run_result["stop_reason"] in _ESCAPE_OUTCOMES:
                break

        agreement = len(signatures) >= 2 and all(
            signature == signatures[0] for signature in signatures[1:])
        return {
            "diagnostic_id": diagnostic_id,
            "attempt_count": len(attempts),
            "max_attempts": max_attempts,
            "max_tool_calls_per_attempt": len(validated_calls),
            "duration_budget_per_attempt_ms": max_duration_ms,
            "total_declared_duration_budget_ms": max_attempts * max_duration_ms,
            "max_pages_per_attempt": max_pages,
            "agreement": agreement,
            "correctness": "NOT_ESTABLISHED",
            "interpretation": "AGREEMENT_IS_NOT_CORRECTNESS",
            "attempts": attempts,
        }

    def run(self, calls: object, *, cost_usd: float | None = None,
            input_tokens: int | None = None, output_tokens: int | None = None,
            max_duration_ms: int = DEFAULT_RUN_DURATION_MS,
            max_pages: int | None = None,
            collection_contract: CollectionContract | None = None,
            _diagnostic_id: str | None = None,
            _diagnostic_attempt: int | None = None) -> dict[str, Any]:
        return self._run(calls, cost_usd=cost_usd, input_tokens=input_tokens,
                         output_tokens=output_tokens, max_duration_ms=max_duration_ms,
                         max_pages=max_pages, collection_contract=collection_contract,
                         _diagnostic_id=_diagnostic_id, _diagnostic_attempt=_diagnostic_attempt)

    def _run(self, calls: object, *, cost_usd: float | None = None,
             input_tokens: int | None = None, output_tokens: int | None = None,
             max_duration_ms: int = DEFAULT_RUN_DURATION_MS,
             max_pages: int | None = None,
             collection_contract: CollectionContract | None = None,
             _diagnostic_id: str | None = None,
             _diagnostic_attempt: int | None = None,
             _prior_slice: tuple[str, int, int] | None = None) -> dict[str, Any]:
        self._validate_metrics(cost_usd, input_tokens, output_tokens)
        if (type(max_duration_ms) is not int or not 1 <= max_duration_ms <= MAX_RUN_DURATION_MS):
            raise ValueError("Agent run duration must be between one and 120000 milliseconds")
        if max_pages is not None and (type(max_pages) is not int or not 1 <= max_pages <= MAX_RUN_PAGES):
            raise ValueError("Agent page budget must be between one and 100 pages")
        if collection_contract is not None and not isinstance(collection_contract, CollectionContract):
            raise ValueError("Agent collection success contract is invalid")
        if (_diagnostic_id is None) != (_diagnostic_attempt is None):
            raise ValueError("Diagnostic trace metadata must include both identifier and attempt")
        if (_diagnostic_id is not None
                and (not isinstance(_diagnostic_id, str)
                     or not re.fullmatch(r"[0-9a-f]{32}", _diagnostic_id)
                     or type(_diagnostic_attempt) is not int
                     or not 1 <= _diagnostic_attempt <= MAX_DIAGNOSTIC_ATTEMPTS)):
            raise ValueError("Diagnostic trace metadata is invalid")
        started = time.monotonic()
        deadline = started + max_duration_ms / 1000
        run_id = new_run_id()
        scope = (self.store, self.registry, self.profile_id, self.task)
        snapshots = []
        step_limit = len(calls) if _prior_slice is not None else MAX_AGENT_STEPS
        profile_ref = sha256(self.profile_id.encode("utf-8")).hexdigest()[:24]
        started_event = {
            "schema_version": 1, "task": self.task, "profile_ref": profile_ref,
            "step_limit": step_limit, "max_duration_ms": max_duration_ms,
            "max_pages": max_pages,
            "success_artifact": "collection_output" if collection_contract else "all_tools_completed",
        }
        if _diagnostic_id is not None:
            started_event.update({"diagnostic_id": _diagnostic_id,
                                  "diagnostic_attempt": _diagnostic_attempt})
        prior_metadata = {}
        if _prior_slice is not None:
            prior_metadata = {"prior_run_id": _prior_slice[0],
                              "slice_start": _prior_slice[1], "slice_stop": _prior_slice[2]}
            started_event.update(prior_metadata)
            started_event["success_artifact"] = "DIAGNOSTIC_ONLY"
        append_agent_event(self.store, run_id, "AGENT_RUN_STARTED", started_event)
        if not isinstance(calls, list):
            calls = []
            state = "REJECTED"
        else:
            state = "COMPLETED"
        results = []
        context = AgentToolContext(run_id=run_id, profile_id=self.profile_id,
                                   task=self.task, deadline_monotonic=deadline,
                                   max_duration_ms=max_duration_ms,
                                   page_budget=PageBudget(max_pages) if max_pages is not None else None)
        stop_reason = None
        collection_result = None
        for index, call in enumerate(calls[:step_limit], start=1):
            if time.monotonic() >= deadline:
                state = "LIMIT_REACHED"
                stop_reason = "TIME_BUDGET_EXHAUSTED"
                break
            name = call.get("name") if isinstance(call, dict) else None
            safe_name = name if isinstance(name, str) and _SAFE_TOOL.fullmatch(name) else "INVALID_TOOL_NAME"
            arguments = call.get("arguments") if isinstance(call, dict) else None
            snapshot = self._read_snapshot(name, arguments)
            snapshots.append(snapshot)
            if snapshot is not None:
                arguments = json.loads(snapshot[1])
            try:
                output, proposal_id = self.registry.invoke(name, arguments, context)
                result_code = "PROPOSAL_PENDING" if proposal_id else "OK"
            except AgentToolError as error:
                output, proposal_id, result_code = {}, None, error.code
                if error.code in _ESCAPE_OUTCOMES:
                    state = error.code
                    stop_reason = error.code
                elif error.code == "TIME_BUDGET_EXHAUSTED":
                    state = "LIMIT_REACHED"
                    stop_reason = error.code
                elif error.code in {"PAGE_BUDGET_REQUIRED", "PAGE_BUDGET_EXHAUSTED"}:
                    state = "LIMIT_REACHED"
                    stop_reason = error.code
                else:
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
            if stop_reason is not None:
                break
            if proposal_id:
                state = "REVIEW_REQUIRED"
                stop_reason = "REVIEW_REQUIRED"
            if time.monotonic() >= deadline:
                state = "LIMIT_REACHED"
                stop_reason = "TIME_BUDGET_EXHAUSTED"
                break
        if len(calls) > step_limit and stop_reason is None:
            state = "LIMIT_REACHED"
            stop_reason = "STEP_BUDGET_EXHAUSTED"
        if state == "COMPLETED" and collection_contract is not None and _prior_slice is None:
            collection_result = validate_collection_output(
                results[-1]["output"] if results else None, collection_contract)
            if collection_result.code != "OUTPUT_VALID":
                state = "OUTPUT_UNVERIFIED"
                stop_reason = collection_result.code
            else:
                stop_reason = "SUCCESS_ARTIFACT_VERIFIED"
        if stop_reason is None:
            stop_reason = state
        if _prior_slice is not None:
            # Never persist a completed/accepted diagnostic run. Keep budget
            # and escape reasons separately without promoting agreement.
            prior_metadata["diagnostic_outcome"] = (
                "CALLS_FINISHED" if stop_reason == "COMPLETED" else stop_reason)
            state, stop_reason = "OUTPUT_UNVERIFIED", "DIAGNOSTIC_ONLY"
        consumed_ms = max(0, int(round((time.monotonic() - started) * 1000)))
        terminal_event = {
            "state": state, "step_count": len(results), "cost_usd": cost_usd,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "stop_reason": stop_reason, "budget_consumed_ms": consumed_ms,
            "max_pages": max_pages,
            "pages_consumed": context.page_budget.consumed if context.page_budget else 0,
            "collection_result": ({"code": collection_result.code,
                                   "observed_count": collection_result.observed_count,
                                   "tail_matches": collection_result.tail_matches}
                                  if collection_result else None),
        }
        if _diagnostic_id is not None:
            terminal_event.update({"diagnostic_id": _diagnostic_id,
                                   "diagnostic_attempt": _diagnostic_attempt})
        terminal_event.update(prior_metadata)
        append_agent_event(self.store, run_id, "AGENT_RUN_TERMINAL", terminal_event)
        if _prior_slice is None:
            self._prior_runs[run_id] = _PriorRunBinding(
                scope, tuple(snapshots), max_duration_ms, max_pages)
            while len(self._prior_runs) > MAX_PRIOR_RUN_BINDINGS:
                del self._prior_runs[next(iter(self._prior_runs))]
        return {"run_id": run_id,
                "state": "DIAGNOSTIC_ONLY" if _prior_slice is not None else state, "steps": results,
                "stop_reason": stop_reason,
                "max_pages": max_pages,
                "pages_consumed": context.page_budget.consumed if context.page_budget else 0,
                "collection_result": ({"code": collection_result.code,
                                       "observed_count": collection_result.observed_count,
                                       "tail_matches": collection_result.tail_matches}
                                      if collection_result else None)}

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
