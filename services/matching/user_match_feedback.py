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


class FeedbackConflict(ValueError):
    """A request ID was already recorded with a different feedback payload."""


def _required_identifier(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _proposal(profile_id: str, job_id: str, category: str) -> dict:
    dimension = _PROPOSAL_DIMENSIONS[category]
    direction = "INCREASE" if category == "GOOD_MATCH" else "DECREASE"
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
