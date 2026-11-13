"""Conservative profile-owned latest terminal outcomes, with no raw details."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import and_, func, or_, select

from packages.persistence import models as m
from services.notifications.contract import NotificationError, NotificationEvent


PROFILE_KINDS = {
    "DISCOVER": "Job discovery",
    "PREPARE": "Document preparation",
    "AI_EVALUATE": "AI evaluation",
    "AI_EVALUATE_QUEUED": "AI evaluation",
}
APPLICATION_KINDS = {
    "APPLY_MOCK": "Fixture application",
    "APPLICATION_DRY_RUN": "Application dry-run",
    "APPLICATION_FORM_REVIEW": "Application form review",
    "UPLOADED_RESUME_COMPARE_QUEUED": "Resume comparison",
}
RESUME_KINDS = {"RESUME_EXTRACT_QUEUED": "Resume extraction"}
_NAMES = {**PROFILE_KINDS, **APPLICATION_KINDS, **RESUME_KINDS}
_OUTCOMES = {
    "COMPLETED": "completed",
    "FAILED": "needs attention",
    "DEAD_LETTERED": "needs review",
    "BLOCKED": "needs review",
    "CANCELLED": "was cancelled",
}


def message_for(kind, state):
    if kind not in _NAMES or state not in _OUTCOMES:
        return None
    return f"{_NAMES[kind]} {_OUTCOMES[state]}. Open MJobs for details."


def event_from_row(row):
    message = message_for(row["kind"], row["state"])
    if message is None or type(row["attempt_count"]) is not int or row["attempt_count"] < 0:
        return None
    key = hashlib.sha256(json.dumps(
        [row["id"], row["kind"], row["state"], row["attempt_count"]],
        separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    return NotificationEvent(key, row["id"], row["kind"], row["state"], row["updated_at"], row["attempt_count"], message)


def read_event_page(store, profile_id, *, activated_at, after_id="", limit=50):
    """A bounded stable-ID sweep; revisits rows to observe later state changes.

    Unknown kinds and unproven ownership are excluded. We observe current
    terminal states, not every intermediate transition between polls.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise NotificationError("NOTIFICATION_BATCH_INVALID")
    with store.engine.connect() as connection:
        if connection.execute(select(m.profiles.c.id).where(
                m.profiles.c.id == profile_id)).scalar_one_or_none() is None:
            raise NotificationError("NOTIFICATION_PROFILE_NOT_FOUND")
        applications = select(m.applications.c.id).where(m.applications.c.profile_id == profile_id)
        resumes = select(m.uploaded_resumes.c.id).where(m.uploaded_resumes.c.profile_id == profile_id)
        param_profile = m.operations.c.params["profile_id"].as_string()
        scope = or_(
            and_(m.operations.c.kind.in_(PROFILE_KINDS),
                 m.operations.c.entity_id == profile_id,
                 or_(param_profile.is_(None), param_profile == profile_id)),
            and_(m.operations.c.kind.in_(APPLICATION_KINDS),
                 m.operations.c.entity_id.in_(applications), param_profile == profile_id),
            and_(m.operations.c.kind.in_(RESUME_KINDS),
                 m.operations.c.entity_id.in_(resumes), param_profile == profile_id),
        )
        rows = list(connection.execute(select(
            m.operations.c.id, m.operations.c.kind, m.operations.c.state, m.operations.c.updated_at,
            m.operations.c.attempt_count,
        ).where(
            scope, m.operations.c.state.in_(_OUTCOMES),
            func.coalesce(m.operations.c.completed_at, m.operations.c.updated_at) >= activated_at,
            m.operations.c.id > after_id,
        ).order_by(m.operations.c.id).limit(limit)).mappings())
    events = tuple(event for row in rows if (event := event_from_row(row)) is not None)
    # A full page advances; a short page resets for the next sweep.
    next_cursor = rows[-1]["id"] if len(rows) == limit else ""
    return events, next_cursor
