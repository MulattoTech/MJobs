"""Persistent ambiguous-duplicate review decisions."""
from __future__ import annotations

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert


ACTIONS = {"MERGE", "KEEP_SEPARATE", "NEVER_MERGE"}


def _pair(left_job_id, right_job_id):
    if not all(isinstance(value, str) and value for value in (left_job_id, right_job_id)):
        raise ValueError("DUPLICATE_JOB_ID_REQUIRED")
    if left_job_id == right_job_id:
        raise ValueError("DUPLICATE_PAIR_IDENTICAL")
    return tuple(sorted((left_job_id, right_job_id)))


def save_review_candidate(store, left_job_id, right_job_id, confidence, evidence):
    left_job_id, right_job_id = _pair(left_job_id, right_job_id)
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        raise ValueError("DUPLICATE_CONFIDENCE_INVALID")
    identifier = identity("duplicate-review", left_job_id, right_job_id)
    timestamp = now()
    with store.engine.begin() as connection:
        connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == left_job_id)).one()
        connection.execute(select(m.jobs.c.id).where(m.jobs.c.id == right_job_id)).one()
        existing = connection.execute(select(m.dedupe_reviews).where(
            m.dedupe_reviews.c.id == identifier)).mappings().one_or_none()
        if existing and existing["decision"] == "NEVER_MERGE":
            return dict(existing)
        created_at = existing["created_at"] if existing else timestamp
        decision = existing["decision"] if existing else None
        state = existing["state"] if existing and decision else "DUPLICATE_REVIEW_REQUIRED"
        upsert(connection, m.dedupe_reviews, {
            "id": identifier,
            "left_job_id": left_job_id,
            "right_job_id": right_job_id,
            "confidence": float(confidence),
            "state": state,
            "evidence": evidence,
            "decision": decision,
            "created_at": created_at,
            "updated_at": timestamp,
        }, replace=True)
        store._audit(connection, identifier, "DUPLICATE_REVIEW_REQUIRED", {
            "left_job_id": left_job_id,
            "right_job_id": right_job_id,
            "confidence": float(confidence),
        })
        return dict(connection.execute(select(m.dedupe_reviews).where(
            m.dedupe_reviews.c.id == identifier)).mappings().one())


def record_review_decision(store, review_id, action):
    action = str(action or "").upper()
    if action not in ACTIONS:
        raise ValueError("DUPLICATE_DECISION_INVALID")
    state = {
        "MERGE": "MERGE_APPROVED",
        "KEEP_SEPARATE": "KEEP_SEPARATE",
        "NEVER_MERGE": "NEVER_MERGE",
    }[action]
    timestamp = now()
    with store.engine.begin() as connection:
        row = connection.execute(select(m.dedupe_reviews).where(
            m.dedupe_reviews.c.id == review_id).with_for_update()).mappings().one()
        connection.execute(m.dedupe_reviews.update().where(
            m.dedupe_reviews.c.id == review_id).values(
                decision=action, state=state, updated_at=timestamp))
        store._audit(connection, review_id, "DUPLICATE_REVIEW_DECIDED", {
            "decision": action,
            "left_job_id": row["left_job_id"],
            "right_job_id": row["right_job_id"],
        })
        return dict(connection.execute(select(m.dedupe_reviews).where(
            m.dedupe_reviews.c.id == review_id)).mappings().one())


def list_duplicate_reviews(store, state=None):
    with store.engine.connect() as connection:
        statement = select(m.dedupe_reviews)
        if state:
            statement = statement.where(m.dedupe_reviews.c.state == state)
        return [dict(row) for row in connection.execute(
            statement.order_by(m.dedupe_reviews.c.updated_at.desc(), m.dedupe_reviews.c.id)).mappings()]
