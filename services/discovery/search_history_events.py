"""Convert terminal discovery operations to search-history service inputs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_TERMINAL_STATUS = {
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "BLOCKED": "FAILED",
    "DEAD_LETTERED": "FAILED",
    "CANCELLED": "CANCELLED",
}


def _as_text(value: Any, *, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not allow_empty and not value:
        return None
    if len(value) > 2000 or any(ord(char) < 32 for char in value):
        return None
    return value


def _iso_utc(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _first_text(*values: Any, allow_empty: bool = False) -> str | None:
    for value in values:
        normalized = _as_text(value, allow_empty=allow_empty)
        if normalized is not None:
            return normalized
    return None


def build_search_history_record(
    operation: Any, metadata: Any = None,
) -> dict | None:
    """Build one ``record_search_history`` input from a terminal DISCOVER op.

    ``metadata`` supplies observed source/query/filter/count/error values. The
    operation supplies its stable identity, profile, terminal status and actual
    attempt timestamps. Missing text descriptors use explicit unknown/empty
    values. Missing counts remain ``None``; missing timestamps return ``None``.
    The operation ID plus attempt number makes repeated event construction
    idempotent at the persistence boundary.
    """
    if not isinstance(operation, dict) or operation.get("kind") != "DISCOVER":
        return None
    params = operation.get("params")
    params = params if isinstance(params, dict) else {}
    if params.get("rescore_only") is True:
        return None

    operation_id = _as_text(operation.get("id"))
    profile_id = _as_text(operation.get("entity_id"))
    attempt = operation.get("attempt_count")
    if (operation_id is None or len(operation_id) > 105
            or profile_id is None or len(profile_id) > 128
            or type(attempt) is not int or attempt < 1):
        return None
    run_id = f"{operation_id}:{attempt}"
    if len(run_id) > 128:
        return None

    state = operation.get("state")
    status = _TERMINAL_STATUS.get(state) if isinstance(state, str) else None
    if status is None:
        return None

    details = metadata if isinstance(metadata, dict) else {}
    started_at = _iso_utc(
        details.get("started_at") or operation.get("last_attempt_at")
        or operation.get("started_at")
    )
    finished_at = _iso_utc(
        details.get("finished_at") or operation.get("dead_lettered_at")
        or operation.get("updated_at")
    )
    if started_at is None or finished_at is None or finished_at < started_at:
        return None

    counts = {}
    for field in ("results", "new_jobs", "duplicates"):
        value = details.get(field)
        counts[field] = value if type(value) is int and value >= 0 else None

    # Keep the source fallback bounded to the persistence contract.
    source_candidates = (details.get("source"), params.get("source_id"))
    source = next((value.strip() for value in source_candidates
                   if isinstance(value, str) and 0 < len(value.strip()) <= 256
                   and not any(ord(char) < 32 for char in value)), "UNKNOWN")

    query = _first_text(details.get("query"), params.get("query"), allow_empty=True)
    if query is None:
        query = ""
    filters = details.get("filters")
    if filters is None:
        filters = params.get("filters", {})
    if not isinstance(filters, dict):
        filters = {}

    errors = details.get("errors")
    if errors is None:
        error = _as_text(operation.get("error"))
        errors = [error] if status in {"FAILED", "CANCELLED"} and error is not None else []
    elif (not isinstance(errors, list) or len(errors) > 50
          or any(not isinstance(error, str) or _as_text(error) is None
                 for error in errors)):
        return None
    operation_error = _as_text(operation.get("error"))
    if status in {"FAILED", "CANCELLED"} and operation_error and operation_error not in errors:
        if len(errors) >= 50:
            return None
        errors = [*errors, operation_error]

    return {
        "run_id": run_id,
        "profile_id": profile_id,
        "source": source,
        "query": query,
        "filters": dict(filters),
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        **counts,
        "errors": [error.strip() for error in errors],
    }


__all__ = ["build_search_history_record"]
