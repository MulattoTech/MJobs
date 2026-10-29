"""Transactional submission admission with durable live-intent evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.domain.safety import Decision, Policy, SubmissionContext, evaluate_submission
from packages.persistence import models as m
from packages.persistence.store import identity, now


GRANTED = "LIVE_SUBMISSION_INTENT_GRANTED"
CONSUMED = "LIVE_SUBMISSION_INTENT_CONSUMED"


def _aware(value: object) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def _grant_id(receipt_id: str) -> str:
    return identity("live-submission-intent-grant", receipt_id)


def _consume_id(receipt_id: str) -> str:
    return identity("live-submission-intent-consume", receipt_id)


def _receipt_id(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 128):
        raise ValueError("LIVE_SUBMISSION_INTENT_INVALID")
    return value


def grant_live_submission_intent(
        store, *, receipt_id: str, profile_id: str, application_id: str,
        job_id: str, granted_at: datetime, expires_at: datetime,
        source_allowed: bool) -> dict:
    """Persist immutable user intent; no public caller is provided here."""
    receipt_id = _receipt_id(receipt_id)
    if (not all(isinstance(value, str) and value for value in (
            profile_id, application_id, job_id))
            or not _aware(granted_at) or not _aware(expires_at)
            or granted_at >= expires_at or type(source_allowed) is not bool):
        raise ValueError("LIVE_SUBMISSION_INTENT_INVALID")
    payload = {
        "receipt_id": receipt_id,
        "profile_id": profile_id,
        "application_id": application_id,
        "job_id": job_id,
        "granted_at": granted_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "source_allowed": source_allowed,
    }
    with store.engine.begin() as connection:
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one()
        if (application["profile_id"] != profile_id or application["job_id"] != job_id
                or application["target"] != "REAL"):
            raise ValueError("LIVE_SUBMISSION_INTENT_SCOPE_MISMATCH")
        connection.execute(m.audit.insert().values(
            id=_grant_id(receipt_id), entity_id=application_id,
            action=GRANTED, payload=payload, timestamp=granted_at.isoformat()))
    return payload


def _load_intent(connection, receipt_id: object, application: dict,
                 evaluated_at: datetime) -> tuple[dict | None, str | None]:
    try:
        normalized_id = _receipt_id(receipt_id)
    except ValueError:
        return None, "DURABLE_JOB_INTENT_REQUIRED"
    row = connection.execute(select(m.audit).where(
        m.audit.c.id == _grant_id(normalized_id),
        m.audit.c.action == GRANTED,
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        return None, "DURABLE_JOB_INTENT_REQUIRED"
    consumed = connection.execute(select(m.audit.c.id).where(
        m.audit.c.id == _consume_id(normalized_id),
        m.audit.c.entity_id == application["id"],
        m.audit.c.action == CONSUMED,
    )).first()
    if consumed is not None:
        return None, "JOB_INTENT_ALREADY_CONSUMED"
    payload = row["payload"]
    if not isinstance(payload, dict):
        return None, "INVALID_JOB_INTENT"
    try:
        granted_at = datetime.fromisoformat(payload["granted_at"])
        expires_at = datetime.fromisoformat(payload["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None, "INVALID_JOB_INTENT"
    expected = {
        "receipt_id": normalized_id,
        "profile_id": application["profile_id"],
        "application_id": application["id"],
        "job_id": application["job_id"],
    }
    if (row["entity_id"] != application["id"]
            or any(payload.get(key) != value for key, value in expected.items())
            or type(payload.get("source_allowed")) is not bool
            or not _aware(granted_at) or not _aware(expires_at)):
        return None, "JOB_INTENT_MISMATCH"
    if not granted_at <= evaluated_at < expires_at:
        return None, "STALE_JOB_INTENT"
    return {
        **expected,
        "granted_at": granted_at,
        "expires_at": expires_at,
        "source_allowed": payload["source_allowed"],
    }, None


def _consume_intent(connection, intent: dict, application: dict,
                    consumed_at: datetime) -> bool:
    values = {
        "id": _consume_id(intent["receipt_id"]),
        "entity_id": application["id"],
        "action": CONSUMED,
        "payload": {
            "receipt_id": intent["receipt_id"],
            "profile_id": application["profile_id"],
            "application_id": application["id"],
            "job_id": application["job_id"],
            "consumed_at": consumed_at.isoformat(),
        },
        "timestamp": consumed_at.isoformat(),
    }
    if connection.dialect.name == "sqlite":
        statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    elif connection.dialect.name == "postgresql":
        statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    else:
        raise ValueError("Unsupported database backend")
    return connection.execute(statement).rowcount == 1


def _context(application: dict, match: dict, confidence: float, hourly: int,
             daily: int, company: int, evaluated_at: datetime,
             intent: dict | None, barrier: str | None) -> SubmissionContext:
    is_live = application["target"] == "REAL"
    return SubmissionContext(
        target="EXTERNAL" if is_live else "MOCK",
        state=application["state"],
        user_enabled=bool(intent) if is_live else False,
        actual_user_intended_job=bool(intent) if is_live else False,
        job_id=application["job_id"] if is_live else "",
        intent_receipt_id=intent["receipt_id"] if intent else "",
        intent_job_id=intent["job_id"] if intent else "",
        intent_granted_at=intent["granted_at"] if intent else None,
        intent_expires_at=intent["expires_at"] if intent else None,
        evaluated_at=evaluated_at if intent else None,
        match_score=match["score"], confidence=confidence,
        quality_approved=True, facts_verified=True,
        source_allowed=(intent["source_allowed"] if intent else not is_live),
        sensitive_answers_approved=True,
        hourly_count=hourly, daily_count=daily, company_count=company,
        barrier=barrier,
    )


def authorize_submission(
        store, application_id: str, confidence: float, policy: Policy = Policy(),
        *, intent_receipt_id: str | None = None,
        evaluated_at: datetime | None = None, barrier: str | None = None,
        check=None) -> Decision:
    """Evaluate and claim one submission in a single database transaction."""
    timestamp = evaluated_at or datetime.now(timezone.utc)
    if not _aware(timestamp):
        raise ValueError("evaluated_at must be timezone-aware")
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = dict(connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one())
        connection.execute(select(m.profiles).where(
            m.profiles.c.id == application["profile_id"]).with_for_update()).one()
        if check:
            check()
        job = dict(connection.execute(select(m.jobs).where(
            m.jobs.c.id == application["job_id"])).mappings().one())
        match = dict(connection.execute(select(m.matches).where(
            m.matches.c.job_id == job["id"],
            m.matches.c.profile_id == application["profile_id"],
            m.matches.c.profile_version == application["profile_version"],
        )).mappings().one())
        submitted = select(func.count()).select_from(
            m.applications.join(m.jobs, m.jobs.c.id == m.applications.c.job_id)
        ).where(
            m.applications.c.profile_id == application["profile_id"],
            m.applications.c.state.in_([
                "SUBMITTING", "CONFIRMED", "SUBMISSION_UNCONFIRMED"]),
        )
        hourly = connection.execute(submitted.where(
            m.applications.c.submitted_at >= (
                timestamp - timedelta(hours=1)).isoformat())).scalar_one()
        daily = connection.execute(submitted.where(
            m.applications.c.submitted_at >= (
                timestamp - timedelta(days=1)).isoformat())).scalar_one()
        company = connection.execute(submitted.where(
            m.jobs.c.company_id == job["company_id"])).scalar_one()
        intent = None
        intent_error = None
        if application["target"] == "REAL":
            intent, intent_error = _load_intent(
                connection, intent_receipt_id, application, timestamp)
        decision = evaluate_submission(_context(
            application, match, confidence, hourly, daily, company,
            timestamp, intent, barrier), policy)
        if intent_error and intent_error not in decision.reasons:
            decision = Decision(False, (*decision.reasons, intent_error))
        if decision.allowed:
            if intent is not None and not _consume_intent(
                    connection, intent, application, timestamp):
                return Decision(False, ("JOB_INTENT_ALREADY_CONSUMED",))
            changed = connection.execute(update(m.applications).where(
                m.applications.c.id == application_id,
                m.applications.c.state == "READY_TO_SUBMIT",
            ).values(state="SUBMITTING", submitted_at=now(),
                     trace=[*application["trace"], "SUBMIT"]))
            if changed.rowcount != 1:
                return Decision(False, ("SUBMISSION_ALREADY_CLAIMED",))
            store._audit(connection, application_id, "SUBMITTING", {
                "provider": "mock-ats" if application["target"] == "MOCK"
                else "live-admission",
            })
        return decision
