"""Durable, profile-scoped history for completed discovery search runs."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json
import math
import re
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity


_ACTIONS = {"SEARCH_RUN_COMPLETED", "SEARCH_RUN_FAILED", "SEARCH_RUN_CANCELLED"}
_RECORD_FIELDS = {
    "run_id", "profile_id", "source", "query", "filters", "started_at",
    "finished_at", "status", "results", "new_jobs", "duplicates", "errors",
}
_MAX_PAGE_SIZE = 100
_MAX_COUNTER = 1_000_000_000
_CURSOR_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_FILTER_KEY = re.compile(r"authorization|cookie|password|secret|token|credential", re.I)
_KNOWN_SAFE_FILTER_KEYS = {"excluded_work_authorization_flags"}


def _text(value: Any, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"SEARCH_HISTORY_{field.upper()}_INVALID")
    value = value.strip()
    if ((not value and not allow_empty) or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"SEARCH_HISTORY_{field.upper()}_INVALID")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError(f"SEARCH_HISTORY_{field.upper()}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"SEARCH_HISTORY_{field.upper()}_INVALID") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"SEARCH_HISTORY_{field.upper()}_INVALID")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _validate_json(value: Any, *, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if value is None or type(value) in (str, bool, int):
        return not isinstance(value, str) or len(value) <= 2000
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return len(value) <= 100 and all(
            _validate_json(item, depth=depth + 1) for item in value
        )
    if isinstance(value, dict):
        return len(value) <= 100 and all(
            isinstance(key, str)
            and 0 < len(key) <= 128
            and (key in _KNOWN_SAFE_FILTER_KEYS or not _SENSITIVE_FILTER_KEY.search(key))
            and _validate_json(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


def _filters(value: Any) -> dict:
    if not isinstance(value, dict) or not _validate_json(value):
        raise ValueError("SEARCH_HISTORY_FILTERS_INVALID")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("SEARCH_HISTORY_FILTERS_INVALID")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError) as error:
        if isinstance(error, ValueError) and str(error) == "SEARCH_HISTORY_FILTERS_INVALID":
            raise
        raise ValueError("SEARCH_HISTORY_FILTERS_INVALID") from error


def _counter(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
        raise ValueError(f"SEARCH_HISTORY_{field.upper()}_INVALID")
    return value


def _normalize_run(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
        raise ValueError("SEARCH_HISTORY_RECORD_INVALID")

    status = value.get("status")
    if not isinstance(status, str) or status not in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise ValueError("SEARCH_HISTORY_STATUS_INVALID")

    started_at = _timestamp(value.get("started_at"), "started_at")
    finished_at = _timestamp(value.get("finished_at"), "finished_at")
    if finished_at < started_at:
        raise ValueError("SEARCH_HISTORY_TIME_RANGE_INVALID")

    errors = value.get("errors")
    if (not isinstance(errors, list) or len(errors) > 50
            or any(not isinstance(error, str) or len(error.strip()) > 1000
                   for error in errors)):
        raise ValueError("SEARCH_HISTORY_ERRORS_INVALID")

    return {
        "run_id": _text(value.get("run_id"), "run_id", maximum=128),
        "profile_id": _text(value.get("profile_id"), "profile_id", maximum=128),
        "source": _text(value.get("source"), "source", maximum=256),
        "query": _text(value.get("query"), "query", maximum=2000, allow_empty=True),
        "filters": _filters(value.get("filters")),
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "results": _counter(value.get("results"), "results"),
        "new_jobs": _counter(value.get("new_jobs"), "new_jobs"),
        "duplicates": _counter(value.get("duplicates"), "duplicates"),
        "errors": [error.strip() for error in errors],
    }


def record_search_history(store, value: dict, *, connection=None) -> dict:
    """Persist one terminal search-run record, idempotently, in audit history.

    The stable run ID makes retries safe. Reusing it with changed data fails
    closed instead of rewriting the original run receipt.
    """
    run = _normalize_run(value)
    store.profile(run["profile_id"])
    action = f"SEARCH_RUN_{run['status']}"
    audit_id = identity("search-history", run["profile_id"], run["run_id"])
    values = {
        "id": audit_id,
        "entity_id": run["profile_id"],
        "action": action,
        "payload": run,
        "timestamp": run["finished_at"],
    }

    def persist(target):
        insert = {
            "sqlite": sqlite_insert,
            "postgresql": postgres_insert,
        }.get(target.dialect.name)
        if insert is None:
            raise ValueError("SEARCH_HISTORY_STORAGE_UNSUPPORTED")
        target.execute(
            insert(m.audit).values(**values).on_conflict_do_nothing(
                index_elements=[m.audit.c.id],
            )
        )
        return target.execute(
            select(m.audit.c.action, m.audit.c.payload).where(m.audit.c.id == audit_id)
        ).mappings().one_or_none()

    if connection is None:
        with store.engine.begin() as transaction:
            existing = persist(transaction)
    else:
        existing = persist(connection)

    if existing is None:
        raise RuntimeError("SEARCH_HISTORY_WRITE_UNCONFIRMED")
    if existing["action"] != action or existing["payload"] != run:
        raise ValueError("SEARCH_HISTORY_RUN_CONFLICT")
    return run


def _encode_cursor(profile_id: str, timestamp: str, event_id: str) -> str:
    value = json.dumps(
        {"profile_id": profile_id, "timestamp": timestamp, "id": event_id},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_cursor(value: Any, profile_id: str) -> tuple[str, str]:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("SEARCH_HISTORY_CURSOR_INVALID")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        cursor = json.loads(decoded)
        if (not isinstance(cursor, dict)
                or set(cursor) != {"profile_id", "timestamp", "id"}
                or cursor["profile_id"] != profile_id
                or not isinstance(cursor["timestamp"], str)
                or not isinstance(cursor["id"], str)
                or not _CURSOR_ID_PATTERN.fullmatch(cursor["id"])):
            raise ValueError
        timestamp = _timestamp(cursor["timestamp"], "cursor")
    except (binascii.Error, UnicodeError, json.JSONDecodeError, KeyError,
            TypeError, ValueError) as error:
        raise ValueError("SEARCH_HISTORY_CURSOR_INVALID") from error
    return timestamp, cursor["id"]


def list_search_history(
    store, profile_id: str, limit: int = 50, cursor: str | None = None,
) -> dict:
    """Read one profile's search-run history using stable keyset pagination."""
    profile_id = _text(profile_id, "profile_id", maximum=128)
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError("SEARCH_HISTORY_LIMIT_INVALID")
    store.profile(profile_id)

    statement = select(m.audit).where(
        m.audit.c.entity_id == profile_id,
        m.audit.c.action.in_(_ACTIONS),
    )
    if cursor is not None:
        timestamp, event_id = _decode_cursor(cursor, profile_id)
        statement = statement.where(or_(
            m.audit.c.timestamp < timestamp,
            and_(m.audit.c.timestamp == timestamp, m.audit.c.id < event_id),
        ))
    statement = statement.order_by(
        m.audit.c.timestamp.desc(), m.audit.c.id.desc(),
    ).limit(limit + 1)

    with store.engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()

    has_more = len(rows) > limit
    page = rows[:limit]
    runs = [dict(row["payload"]) for row in page]
    next_cursor = (
        _encode_cursor(profile_id, page[-1]["timestamp"], page[-1]["id"])
        if has_more and page else None
    )
    return {"profile_id": profile_id, "runs": runs, "next_cursor": next_cursor}


__all__ = ["list_search_history", "record_search_history"]
