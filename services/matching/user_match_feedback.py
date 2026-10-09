"""Append-only, profile-scoped feedback for matched jobs.

Feedback is stored as an audit event and may produce a deterministic preference
proposal. This service never edits saved preferences, match scores, eligibility,
or application state.
"""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.persistence import models as m
from packages.persistence.store import identity, now


FEEDBACK_CATEGORIES = frozenset({
    "GOOD_MATCH",
    "BAD_MATCH",
    "TOO_SENIOR",
    "WRONG_TECHNOLOGY",
    "WRONG_LOCATION",
    "BAD_SALARY",
    "BAD_COMPANY",
    "NOT_INTERESTED",
})

_PROPOSAL_DIMENSIONS = {
    "GOOD_MATCH": "overall_fit",
    "BAD_MATCH": "overall_fit",
    "TOO_SENIOR": "seniority",
    "WRONG_TECHNOLOGY": "technology",
    "WRONG_LOCATION": "location",
    "BAD_SALARY": "salary",
    "BAD_COMPANY": "company",
    "NOT_INTERESTED": "overall_fit",
}
_PROPOSAL_DIRECTIONS = {
    "GOOD_MATCH": "INCREASE",
    "BAD_MATCH": "DECREASE",
    "TOO_SENIOR": "INCREASE",
    "WRONG_TECHNOLOGY": "INCREASE",
    "WRONG_LOCATION": "INCREASE",
    "BAD_SALARY": "INCREASE",
    "BAD_COMPANY": "INCREASE",
    "NOT_INTERESTED": "REVIEW",
}
_REVIEWABLE_DIMENSIONS = frozenset({
    "overall", "overall_fit", "seniority", "technology", "location", "salary", "company",
})
_APPLIED_DIRECTIONS = frozenset({"increase", "decrease"})


class FeedbackConflict(ValueError):
    """A request ID was already recorded with a different feedback payload."""


def _required_identifier(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _proposal(profile_id: str, job_id: str, category: str) -> dict:
    dimension = _PROPOSAL_DIMENSIONS[category]
    direction = _PROPOSAL_DIRECTIONS[category]
    return {
        "advisory": True,
        "profile_id": profile_id,
        "job_id": job_id,
        "category": category,
        "dimension": dimension,
        "direction": direction,
        "explanation": f"{category.replace('_', ' ').title()} feedback suggests {direction.lower()} emphasis on {dimension}.",
        "review_required": True,
        "requires_confirmation": True,
    }


def _append_once(connection, values: dict) -> None:
    dialect = connection.dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    elif dialect == "postgresql":
        statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    else:
        raise ValueError("Unsupported persistence backend")
    result = connection.execute(statement)
    return result.rowcount == 1


def record_feedback(store, profile_id, job_id, category, request_id) -> dict:
    """Append feedback once and return its stable, non-mutating proposal.

    The job must have a current match for the specified profile. Repeating a
    request with the same job and category returns the original event; changing
    either under that request ID raises :class:`FeedbackConflict`.
    """
    profile_id = _required_identifier(profile_id, "profile_id")
    job_id = _required_identifier(job_id, "job_id")
    request_id = _required_identifier(request_id, "request_id")
    if not isinstance(category, str) or category not in FEEDBACK_CATEGORIES:
        raise ValueError("Unsupported match feedback category")

    event_id = identity("user-match-feedback", 1, profile_id, request_id)
    proposal = _proposal(profile_id, job_id, category)
    payload = {
        "job_id": job_id,
        "category": category,
        "request_id": request_id,
        "proposal": proposal,
    }

    with store.engine.begin() as connection:
        profile = connection.execute(select(m.profiles.c.id, m.profiles.c.version).where(
            m.profiles.c.id == profile_id)).mappings().one_or_none()
        if profile is None:
            raise LookupError("Feedback profile not found")
        job_exists = connection.execute(select(m.jobs.c.id).where(
            m.jobs.c.id == job_id)).scalar_one_or_none()
        if job_exists is None:
            raise LookupError("Feedback job not found")
        match_exists = connection.execute(select(m.matches.c.id).where(
            m.matches.c.profile_id == profile_id,
            m.matches.c.job_id == job_id,
            m.matches.c.profile_version == profile["version"],
        )).scalar_one_or_none()
        if match_exists is None:
            raise LookupError("Job is not currently matched to this profile")

        created_at = now()
        inserted = _append_once(connection, {
            "id": event_id,
            "entity_id": profile_id,
            "action": "MATCH_FEEDBACK",
            "payload": payload,
            "timestamp": created_at,
        })
        stored = connection.execute(select(
            m.audit.c.id, m.audit.c.payload, m.audit.c.timestamp,
        ).where(m.audit.c.id == event_id,
                m.audit.c.entity_id == profile_id,
                m.audit.c.action == "MATCH_FEEDBACK")).mappings().one()

    stored_payload = stored["payload"]
    if not isinstance(stored_payload, Mapping):
        raise FeedbackConflict("Existing feedback request has an invalid payload")
    if (stored_payload.get("job_id") != job_id
            or stored_payload.get("category") != category
            or stored_payload.get("request_id") != request_id):
        raise FeedbackConflict("Request ID was already used for different feedback")

    return {
        "profile_id": profile_id,
        "job_id": job_id,
        "feedback": {
            "id": stored["id"],
            "category": category,
            "created_at": stored["timestamp"],
        },
        "replayed": not inserted,
        "preference_proposal": dict(stored_payload["proposal"]),
    }


def list_feedback(store, profile_id: str, job_id: str) -> dict:
    """Return append-only feedback history for an existing profile/job pair."""
    profile_id = _required_identifier(profile_id, "profile_id")
    job_id = _required_identifier(job_id, "job_id")
    with store.engine.connect() as connection:
        profile_exists = connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id)).scalar_one_or_none()
        job_exists = connection.execute(select(m.jobs.c.id).where(
            m.jobs.c.id == job_id)).scalar_one_or_none()
        if profile_exists is None or job_exists is None:
            raise LookupError("Feedback profile or job not found")
        rows = connection.execute(select(
            m.audit.c.id, m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == "MATCH_FEEDBACK",
        ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings().all()
    items = []
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, Mapping) or payload.get("job_id") != job_id:
            continue
        category = payload.get("category")
        if category not in FEEDBACK_CATEGORIES:
            continue
        items.append({"id": row["id"], "category": category, "created_at": row["timestamp"]})
    return {"profile_id": profile_id, "job_id": job_id, "items": items,
            "latest": items[-1] if items else None}


def apply_feedback_preference(store, profile_id, feedback_id, dimension, direction,
                              request_id) -> dict:
    """Append an explicit user decision to use one saved feedback proposal.

    This is separate from feedback capture: callers must supply the ID of an
    existing profile-owned feedback event, an allowed proposal dimension, and
    an explicit INCREASE/DECREASE choice. The immutable event can influence
    advisory priority only; it never mutates profile filters or eligibility.
    """
    profile_id = _required_identifier(profile_id, "profile_id")
    feedback_id = _required_identifier(feedback_id, "feedback_id")
    request_id = _required_identifier(request_id, "request_id")
    if not isinstance(dimension, str) or dimension not in _REVIEWABLE_DIMENSIONS:
        raise ValueError("Unsupported reviewed preference dimension")
    dimension = "overall" if dimension == "overall_fit" else dimension
    if not isinstance(direction, str) or direction.casefold() not in _APPLIED_DIRECTIONS:
        raise ValueError("An explicit increase or decrease is required")
    direction = direction.upper()

    with store.engine.begin() as connection:
        profile_exists = connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id)).scalar_one_or_none()
        if profile_exists is None:
            raise LookupError("Preference profile not found")
        feedback = connection.execute(select(
            m.audit.c.id, m.audit.c.payload,
        ).where(
            m.audit.c.id == feedback_id,
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == "MATCH_FEEDBACK",
        )).mappings().one_or_none()
        if feedback is None or not isinstance(feedback["payload"], Mapping):
            raise LookupError("Reviewed feedback was not found for this profile")
        feedback_payload = feedback["payload"]
        category = feedback_payload.get("category")
        job_id = feedback_payload.get("job_id")
        if category not in FEEDBACK_CATEGORIES or not isinstance(job_id, str):
            raise ValueError("Stored feedback cannot support a preference")

        event_id = identity("user-match-preference", 1, profile_id, request_id)
        payload = {
            "job_id": job_id,
            "feedback_id": feedback_id,
            "category": category,
            "dimension": dimension,
            "direction": direction,
            "request_id": request_id,
        }
        inserted = _append_once(connection, {
            "id": event_id,
            "entity_id": profile_id,
            "action": "MATCH_PREFERENCE_APPLIED",
            "payload": payload,
            "timestamp": now(),
        })
        stored = connection.execute(select(
            m.audit.c.id, m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.id == event_id,
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == "MATCH_PREFERENCE_APPLIED",
        )).mappings().one()

    if stored["payload"] != payload:
        raise FeedbackConflict("Preference request ID was reused with different review content")
    return {
        "profile_id": profile_id,
        "replayed": not inserted,
        "preference": {
            "id": stored["id"],
            "job_id": job_id,
            "feedback_id": feedback_id,
            "category": category,
            "dimension": dimension,
            "direction": direction,
            "created_at": stored["timestamp"],
        },
    }


def list_feedback_preferences(store, profile_id: str) -> dict:
    """Return profile-scoped reviewed preferences with latest values by dimension."""
    profile_id = _required_identifier(profile_id, "profile_id")
    with store.engine.connect() as connection:
        exists = connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id)).scalar_one_or_none()
        if exists is None:
            raise LookupError("Preference profile not found")
        rows = connection.execute(select(
            m.audit.c.id, m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == "MATCH_PREFERENCE_APPLIED",
        ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings().all()

    items = []
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, Mapping):
            continue
        dimension = payload.get("dimension")
        direction = payload.get("direction")
        if dimension not in _REVIEWABLE_DIMENSIONS or direction not in {"INCREASE", "DECREASE"}:
            continue
        items.append({
            "id": row["id"],
            "job_id": payload.get("job_id"),
            "feedback_id": payload.get("feedback_id"),
            "category": payload.get("category"),
            "dimension": "overall" if dimension == "overall_fit" else dimension,
            "direction": direction,
            "created_at": row["timestamp"],
        })
    by_dimension = {}
    for item in items:
        by_dimension[item["dimension"]] = item
    return {"profile_id": profile_id, "items": items, "by_dimension": by_dimension}
