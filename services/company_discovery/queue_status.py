"""Bounded, profile-scoped status projection for durable company discovery work."""
from __future__ import annotations

from contextlib import nullcontext

from sqlalchemy import func, select

from packages.persistence import models as m
from packages.persistence.store import identity


_STATE_BUCKETS = {
    "PENDING": "queued",
    "QUEUED": "queued",
    "RUNNING": "running",
    "SUCCEEDED": "succeeded",
    "COMPLETED": "succeeded",
    "FAILED": "needs_review",
    "BLOCKED": "needs_review",
    "DEAD_LETTERED": "needs_review",
    "NEEDS_REVIEW": "needs_review",
    "CANCELLED": "cancelled",
    "DISCARDED": "cancelled",
}
_ACTIVITY_ACTIONS = {
    "COMPANY_DISCOVERED": "companies_discovered",
    "COMPANY_SOURCE_ENROLLED": "sources_enrolled",
}


def _safe_attempt_count(value: object) -> int:
    if type(value) is int and 0 <= value <= 1_000_000:
        return value
    return 0


def read_company_discovery_queue_status(store, profile_id: str, *, limit: int = 100) -> dict:
    """Summarize durable company-discovery operations visible to one profile.

    Operations are included only when their persisted ``params.profile_id``
    exactly matches the requested profile. The response omits operation IDs,
    params, errors, provider data, URLs, and audit payloads.
    """
    store.profile(profile_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("COMPANY_DISCOVERY_QUEUE_LIMIT_INVALID")
    limit = min(limit, 200)

    context = store.engine.connect() if hasattr(store, "engine") else nullcontext(store)
    with context as connection:
        scoped_operations = (
            (m.operations.c.kind == "COMPANY_DISCOVER")
            & (m.operations.c.params["profile_id"].as_string() == profile_id)
        )
        state_counts = connection.execute(select(
            m.operations.c.state, func.count().label("count"),
        ).where(scoped_operations).group_by(m.operations.c.state)).all()
        counts = {
            "total": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "needs_review": 0,
            "cancelled": 0,
            "unknown": 0,
        }
        for raw_state, raw_count in state_counts:
            count = int(raw_count)
            counts["total"] += count
            bucket = _STATE_BUCKETS.get(raw_state)
            counts[bucket or "unknown"] += count

        rows = connection.execute(select(
            m.operations.c.id,
            m.companies.c.name.label("company"),
            m.operations.c.params["dispatch_priority"].as_string().label("priority"),
            m.operations.c.state,
            m.operations.c.updated_at,
            m.operations.c.attempt_count,
        ).select_from(m.operations.outerjoin(
            m.companies, m.companies.c.id == m.operations.c.entity_id,
        )).where(scoped_operations).order_by(
            m.operations.c.updated_at.desc(), m.operations.c.id.desc(),
        ).limit(limit)).mappings()
        items = []
        for row in rows:
            raw_priority = row["priority"]
            priority = "HIGH" if raw_priority == "HIGH" else "NORMAL"
            raw_state = row["state"]
            state = {
                "queued": "QUEUED",
                "running": "RUNNING",
                "succeeded": "SUCCEEDED",
                "needs_review": "NEEDS_REVIEW",
                "cancelled": "CANCELLED",
            }.get(_STATE_BUCKETS.get(raw_state), "UNKNOWN")
            items.append({
                "id": identity("company-discovery-queue", profile_id, row["id"]),
                "company": row["company"] if isinstance(row["company"], str) else "Company discovery",
                "state": state,
                "priority": priority,
                "updated_at": row["updated_at"],
                "attempt_count": _safe_attempt_count(row["attempt_count"]),
            })

        activity_counts = {value: 0 for value in _ACTIVITY_ACTIONS.values()}
        audit_counts = connection.execute(select(
            m.audit.c.action, func.count().label("count"),
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action.in_(tuple(_ACTIVITY_ACTIONS)),
        ).group_by(m.audit.c.action)).all()
        for action, count in audit_counts:
            activity_counts[_ACTIVITY_ACTIONS[action]] = int(count)

    return {"counts": counts, "activity_counts": activity_counts, "items": items}
