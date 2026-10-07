"""Bounded value-free agent trace persistence over append-only audit events."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from sqlalchemy import select, text

from packages.persistence import models as m


MAX_AGENT_STEPS = 20
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL_STATES = {"COMPLETED", "REVIEW_REQUIRED", "REJECTED", "FAILED", "CANCELLED",
                    "LIMIT_REACHED", "AUTH_REQUIRED", "NOT_AVAILABLE", "SOURCE_CHANGED",
                    "OUTPUT_UNVERIFIED"}
_STEP_RESULT_CODES = {"OK", "PROPOSAL_PENDING", "UNKNOWN_TOOL", "INVALID_ARGUMENTS",
                      "ARGUMENTS_TOO_LARGE", "INVALID_TOOL_RESULT", "RESULT_TOO_LARGE", "TOOL_FAILED",
                      "TIME_BUDGET_EXHAUSTED", "AUTH_REQUIRED", "NOT_AVAILABLE", "SOURCE_CHANGED"}


def new_run_id() -> str:
    return uuid4().hex


def _entity(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid agent run identifier")
    return "agent-run:" + run_id


def _validate_cost(cost_usd):
    if cost_usd is None:
        return None
    if type(cost_usd) not in (int, float) or not 0 <= cost_usd <= 100:
        raise ValueError("Agent cost metadata must be between zero and one hundred dollars")
    return float(cost_usd)


def _validate_tokens(value):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise ValueError("Agent token metadata is outside bounds")
    return value


def _lock(connection, run_id: str):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        lock_key = int.from_bytes(bytes.fromhex(run_id[:16]), "big") & 0x7FFFFFFFFFFFFFFF
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})


def _rows(connection, run_id: str):
    return list(connection.execute(
        select(m.audit).where(m.audit.c.entity_id == _entity(run_id),
                              m.audit.c.action.like("AGENT_%"))
        .order_by(m.audit.c.timestamp, m.audit.c.id)
    ).mappings())


def append_agent_event(store, run_id: str, action: str, payload: dict) -> dict:
    if action not in {
        "AGENT_RUN_STARTED", "AGENT_TOOL_STEP", "AGENT_RUN_TERMINAL",
        "AGENT_PROPOSAL_REVIEWED", "AGENT_PROPOSAL_EXECUTION_STARTED",
        "AGENT_PROPOSAL_EXECUTED", "AGENT_PROPOSAL_REJECTED",
    }:
        raise ValueError("Unsupported agent trace event")
    if not isinstance(payload, dict):
        raise ValueError("Agent trace event payload must be an object")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Agent trace event must contain bounded JSON data") from error
    if len(encoded.encode("utf-8")) > 4_096:
        raise ValueError("Agent trace event exceeds the storage bound")
    entity = _entity(run_id)
    with store.engine.begin() as connection:
        _lock(connection, run_id)
        rows = _rows(connection, run_id)
        if action != "AGENT_RUN_STARTED" and not any(row["action"] == "AGENT_RUN_STARTED" for row in rows):
            raise ValueError("Agent trace must start before appending events")
        if any(row["action"] == "AGENT_RUN_TERMINAL" for row in rows) and action not in {
                "AGENT_PROPOSAL_REVIEWED", "AGENT_PROPOSAL_EXECUTION_STARTED",
                "AGENT_PROPOSAL_EXECUTED", "AGENT_PROPOSAL_REJECTED"}:
            raise ValueError("Agent run already has a terminal result")
        if action == "AGENT_RUN_STARTED" and rows:
            raise ValueError("Agent run already exists")
        if action == "AGENT_TOOL_STEP":
            step = payload.get("step")
            steps = [row for row in rows if row["action"] == "AGENT_TOOL_STEP"]
            if type(step) is not int or step != len(steps) + 1 or step > MAX_AGENT_STEPS:
                raise ValueError("Agent step exceeds the run bound")
            if payload.get("result_code") not in _STEP_RESULT_CODES:
                raise ValueError("Agent step result code is invalid")
        if action == "AGENT_RUN_TERMINAL":
            if payload.get("state") not in _TERMINAL_STATES:
                raise ValueError("Agent run needs an explicit terminal state")
            payload = {**payload, "cost_usd": _validate_cost(payload.get("cost_usd")),
                       "input_tokens": _validate_tokens(payload.get("input_tokens")),
                       "output_tokens": _validate_tokens(payload.get("output_tokens"))}
            duration = payload.get("budget_consumed_ms")
            if type(duration) is not int or duration < 0:
                raise ValueError("Agent run needs a non-negative consumed-duration value")
        store._audit(connection, entity, action, payload)
    return {"entity_id": entity, "action": action, "payload": payload}


def read_agent_trace(store, run_id: str) -> dict:
    _entity(run_id)
    with store.engine.connect() as connection:
        rows = _rows(connection, run_id)
    if not rows or rows[0]["action"] != "AGENT_RUN_STARTED":
        raise ValueError("Agent run is unavailable")
    result = {"run_id": run_id, "started_at": rows[0]["timestamp"], "steps": [],
              "terminal": None, "proposal_events": []}
    for row in rows[1:]:
        payload = dict(row["payload"])
        if row["action"] == "AGENT_TOOL_STEP":
            result["steps"].append(payload)
        elif row["action"] == "AGENT_RUN_TERMINAL":
            result["terminal"] = payload
        else:
            result["proposal_events"].append({"action": row["action"], **payload})
    return result
