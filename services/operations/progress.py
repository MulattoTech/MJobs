"""Validated durable progress updates and a content-minimizing operation read model."""
import re

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import now


_STEP_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RECOVERY_KEYS = {"action", "reason_code", "next_step", "retry_after_seconds", "retryable"}
_RECOVERY_ACTIONS = {"RETRY", "RESUME", "RECONCILE", "MANUAL_REVIEW", "NONE"}
_TERMINAL_STATES = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "DEAD_LETTERED", "DISCARDED"}
_TRANSITIONS = {
    "QUEUED": {"QUEUED", "RUNNING", "CANCELLED", "BLOCKED"},
    "RUNNING": {"RUNNING", *_TERMINAL_STATES},
}


def _validate_code(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _ERROR_CODE.fullmatch(value):
        raise ValueError(f"{label}_INVALID")
    return value


def _sanitize_recovery_metadata(value):
    if not isinstance(value, dict) or set(value) - _RECOVERY_KEYS or len(value) > len(_RECOVERY_KEYS):
        raise ValueError("RECOVERY_METADATA_INVALID")
    cleaned = {}
    if "action" in value:
        action = value["action"]
        if not isinstance(action, str) or action not in _RECOVERY_ACTIONS:
            raise ValueError("RECOVERY_METADATA_INVALID")
        cleaned["action"] = action
    for key in ("reason_code", "next_step"):
        if key in value:
            cleaned[key] = _validate_code(value[key], "RECOVERY_METADATA")
    if "retry_after_seconds" in value:
        delay = value["retry_after_seconds"]
        if type(delay) is not int or not 0 <= delay <= 86400:
            raise ValueError("RECOVERY_METADATA_INVALID")
        cleaned["retry_after_seconds"] = delay
    if "retryable" in value:
        if type(value["retryable"]) is not bool:
            raise ValueError("RECOVERY_METADATA_INVALID")
        cleaned["retryable"] = value["retryable"]
    return cleaned


def _recovery_summary(value):
    metadata = value if isinstance(value, dict) else {}
    action = metadata.get("action")
    reason = metadata.get("reason_code")
    if action and reason:
        return f"{action}: {reason}"
    return action or reason or ""


def _read_model(row):
    metadata = _sanitize_recovery_metadata(row["recovery_metadata"] or {})
    current_step = row["current_step"]
    if current_step is not None and (
        not isinstance(current_step, str) or not _STEP_CODE.fullmatch(current_step)
    ):
        current_step = None
    return {
        "operation_id": row["id"],
        "operation_type": row["kind"],
        "entity_id": row["entity_id"],
        "state": row["state"],
        "current_step": current_step,
        "progress": row["progress"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "retry_count": row["retry_count"],
        "recovery_summary": _recovery_summary(metadata),
    }


def get_operation_progress(store, operation_id):
    """Read one operation's safe status fields without params, errors, or metadata."""
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise ValueError("OPERATION_ID_INVALID")
    with store.engine.connect() as connection:
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id)).mappings().one_or_none()
    if row is None:
        raise ValueError("OPERATION_NOT_FOUND")
    return _read_model(row)


def record_operation_progress(store, operation_id, *, current_step, progress, state=None,
                              error=None, retry_count=None, recovery_metadata=None):
    """Persist monotonic progress and validated lifecycle metadata atomically."""
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise ValueError("OPERATION_ID_INVALID")
    if current_step is not None and (not isinstance(current_step, str)
                                     or not _STEP_CODE.fullmatch(current_step)):
        raise ValueError("CURRENT_STEP_INVALID")
    if type(progress) is not int or not 0 <= progress <= 100:
        raise ValueError("PROGRESS_INVALID")
    if state is not None and (
        not isinstance(state, str) or state not in _TERMINAL_STATES | {"QUEUED", "RUNNING"}
    ):
        raise ValueError("OPERATION_STATE_INVALID")
    if error is not None:
        _validate_code(error, "OPERATION_ERROR")
    if retry_count is not None and (type(retry_count) is not int or retry_count < 0):
        raise ValueError("RETRY_COUNT_INVALID")
    clean_metadata = None if recovery_metadata is None else _sanitize_recovery_metadata(recovery_metadata)

    timestamp = now()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id).with_for_update()).mappings().one_or_none()
        if row is None:
            raise ValueError("OPERATION_NOT_FOUND")
        row = dict(row)
        if row["state"] in _TERMINAL_STATES:
            raise ValueError("OPERATION_TERMINAL")
        next_state = row["state"] if state is None else state
        if next_state not in _TRANSITIONS.get(row["state"], set()):
            raise ValueError("OPERATION_TRANSITION_INVALID")
        if progress < row["progress"]:
            raise ValueError("PROGRESS_REGRESSION")
        next_retry_count = row["retry_count"] if retry_count is None else retry_count
        if next_retry_count < row["retry_count"]:
            raise ValueError("RETRY_COUNT_REGRESSION")
        if next_state == "COMPLETED" and progress != 100:
            raise ValueError("COMPLETED_PROGRESS_INVALID")

        next_step = row["current_step"] if current_step is None else current_step
        values = {
            "current_step": next_step,
            "progress": progress,
            "state": next_state,
            "updated_at": timestamp,
            "retry_count": next_retry_count,
        }
        if error is not None:
            values["error"] = error
        elif next_state == "COMPLETED":
            values["error"] = None
        if clean_metadata is not None:
            values["recovery_metadata"] = clean_metadata
        if next_state == "RUNNING" and row["started_at"] is None:
            values["started_at"] = timestamp
        if next_state in _TERMINAL_STATES:
            values["completed_at"] = timestamp
        connection.execute(update(m.operations).where(
            m.operations.c.id == operation_id,
            m.operations.c.state == row["state"],
        ).values(**values))
        updated = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id)).mappings().one()
        store._audit(connection, operation_id, "OPERATION_PROGRESS_UPDATED", {
            "state": next_state,
            "current_step": next_step,
            "progress": progress,
            "retry_count": next_retry_count,
            "recovery_summary": _recovery_summary(clean_metadata if clean_metadata is not None
                                                   else row["recovery_metadata"]),
        })
    return _read_model(updated)


__all__ = ["get_operation_progress", "record_operation_progress"]
