"""Durable request identity for profile-scoped pipeline preparation actions."""
from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy import select, update

from packages.persistence import models as m


_ACTION_FINGERPRINT_KEY = "pipeline_workbench_action_fingerprint"
_SAFE_STATES = frozenset({
    "QUEUED", "RUNNING", "COMPLETED", "FAILED", "BLOCKED", "CANCELLED",
    "DEAD_LETTERED", "DISCARDED",
})


def action_fingerprint(*, profile_id: str, job_id: str, request_id: str,
                       expected_profile_version: str, readiness_fingerprint: str) -> str:
    """Bind a request key to the exact readiness snapshot the user approved."""
    payload = {
        "profile_id": profile_id,
        "job_id": job_id,
        "request_id": request_id,
        "expected_profile_version": expected_profile_version,
        "readiness_fingerprint": readiness_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def find_prepare_request(store, profile_id: str, request_id: str):
    """Find a previous profile-scoped PREPARE request without returning its payload."""
    with store.engine.connect() as connection:
        rows = connection.execute(select(m.operations).where(
            m.operations.c.kind == "PREPARE",
            m.operations.c.entity_id == profile_id,
        ).order_by(m.operations.c.updated_at.desc(), m.operations.c.id.desc())).mappings()
        for row in rows:
            params = row["params"] if isinstance(row["params"], dict) else {}
            if params.get("request_id") == request_id:
                return dict(row)
    return None


def bind_prepare_request(store, operation_id: str, request_fingerprint: str, *,
                         profile_id: str, job_id: str, request_id: str,
                         expected_profile_version: str) -> dict:
    """Persist the workbench request binding beside the durable PREPARE outbox row."""
    with store.engine.begin() as connection:
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id,
            m.operations.c.kind == "PREPARE",
        ).with_for_update()).mappings().one_or_none()
        if row is None:
            raise ValueError("PIPELINE_PREPARE_OPERATION_NOT_FOUND")
        params = row["params"] if isinstance(row["params"], dict) else {}
        if (row["entity_id"] != profile_id
                or params.get("profile_id") != profile_id
                or params.get("job_id") != job_id
                or params.get("request_id") != request_id
                or params.get("profile_version") != expected_profile_version):
            raise ValueError("PIPELINE_PREPARE_OPERATION_SCOPE_MISMATCH")
        previous = params.get(_ACTION_FINGERPRINT_KEY)
        if previous is not None and (
                not isinstance(previous, str)
                or not hmac.compare_digest(previous, request_fingerprint)):
            raise ValueError("PIPELINE_PREPARE_REQUEST_CONFLICT")
        if previous is None:
            params = {**params, _ACTION_FINGERPRINT_KEY: request_fingerprint}
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id,
            ).values(params=params))
        return {"operation_id": row["id"], "state": row["state"]}


def replay_prepare_request(row, request_fingerprint: str) -> dict:
    """Validate request-key reuse and return only safe operation acknowledgement fields."""
    params = row["params"] if isinstance(row["params"], dict) else {}
    previous = params.get(_ACTION_FINGERPRINT_KEY)
    if (not isinstance(previous, str)
            or not hmac.compare_digest(previous, request_fingerprint)):
        raise ValueError("PIPELINE_PREPARE_REQUEST_CONFLICT")
    state = row["state"]
    if not isinstance(state, str) or state not in _SAFE_STATES:
        raise ValueError("PIPELINE_PREPARE_STATE_INVALID")
    return {"operation_id": row["id"], "state": state}


__all__ = [
    "action_fingerprint", "bind_prepare_request", "find_prepare_request",
    "replay_prepare_request",
]
