"""Profile-scoped, append-only audit primitives for document archives."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.persistence import models as m
from packages.persistence.store import identity, now


ARCHIVE_ACTIONS = frozenset({"DOCUMENT_ARCHIVED", "DOCUMENT_ARCHIVE_RESTORED"})
_MAX_PAYLOAD_BYTES = 16 * 1024


def append_archive_event(
    store,
    *,
    profile_id: str,
    application_id: str | None = None,
    document_id: str,
    action: str,
    operation_id: str,
    payload: dict | None = None,
    connection=None,
) -> dict:
    """Append one archive event, idempotently, after verifying document ownership.

    Generated documents are scoped through their application. Uploaded resume
    revisions are profile-only and use ``application_id=None``. A retry with the
    same operation identity is a no-op only when its immutable payload matches.
    When ``connection`` is supplied, the caller retains transaction ownership.
    """
    _validate_identifier("profile_id", profile_id)
    if application_id is not None:
        _validate_identifier("application_id", application_id)
    _validate_identifier("document_id", document_id)
    _validate_identifier("operation_id", operation_id)
    _validate_action(action)
    event_payload = _payload(profile_id, application_id, document_id, operation_id, payload)
    entity_id = application_id or profile_id
    event_id = identity("document-archive-audit", profile_id, application_id, action, operation_id)

    def append(conn):
        _verify_document_owner(conn, profile_id, application_id, document_id)
        existing = conn.execute(select(m.audit).where(m.audit.c.id == event_id)).mappings().first()
        if existing is not None:
            return _same_event(existing, event_payload, profile_id, application_id,
                               document_id, action, operation_id)

        timestamp = _next_timestamp(conn, entity_id)
        values = {
            "id": event_id,
            "entity_id": entity_id,
            "action": action,
            "payload": event_payload,
            "timestamp": timestamp,
        }
        dialect = conn.dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
                index_elements=[m.audit.c.id]
            )
        elif dialect == "postgresql":
            statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
                index_elements=[m.audit.c.id]
            )
        else:
            raise ValueError("Unsupported persistence backend")
        conn.execute(statement)

        # A concurrent identical retry may have won the insert. Verify the row
        # after the conflict so a reused id can never hide different event data.
        saved = conn.execute(select(m.audit).where(m.audit.c.id == event_id)).mappings().one()
        return _same_event(saved, event_payload, profile_id, application_id,
                           document_id, action, operation_id)

    if connection is not None:
        return append(connection)
    with store.engine.begin() as conn:
        return append(conn)


def list_archive_events(
    store,
    *,
    profile_id: str,
    application_id: str | None = None,
    document_id: str | None = None,
    limit: int = 100,
    connection=None,
) -> list[dict]:
    """Read archive events for exactly one profile/application/document scope."""
    _validate_identifier("profile_id", profile_id)
    if application_id is not None:
        _validate_identifier("application_id", application_id)
    if document_id is not None:
        _validate_identifier("document_id", document_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Archive event limit must be between 1 and 1000")

    entity_id = application_id or profile_id

    def read(conn):
        _verify_document_owner(conn, profile_id, application_id, document_id)
        rows = conn.execute(select(
            m.audit.c.id, m.audit.c.entity_id, m.audit.c.action,
            m.audit.c.payload, m.audit.c.timestamp,
        ).where(
            m.audit.c.entity_id == entity_id,
            m.audit.c.action.in_(ARCHIVE_ACTIONS),
        ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings()
        events = []
        for row in rows:
            payload = row["payload"] or {}
            if (payload.get("profile_id") != profile_id
                    or payload.get("application_id") != application_id
                    or (document_id is not None and payload.get("document_id") != document_id)):
                continue
            events.append({
                "id": row["id"],
                "profile_id": profile_id,
                "application_id": application_id,
                "document_id": payload.get("document_id"),
                "operation_id": payload.get("operation_id"),
                "action": row["action"],
                "payload": payload,
                "timestamp": row["timestamp"],
            })
        return events[-limit:]

    if connection is not None:
        return read(connection)
    with store.engine.connect() as conn:
        return read(conn)


def _verify_document_owner(connection, profile_id: str, application_id: str | None,
                           document_id: str | None) -> None:
    if application_id is None:
        profile_exists = connection.execute(select(m.career_profiles.c.id).where(
            m.career_profiles.c.id == profile_id
        )).first()
        if profile_exists is None:
            raise LookupError("Document is not available to this profile")
        if document_id is not None:
            revision = connection.execute(select(m.resume_revisions.c.id).where(
                m.resume_revisions.c.id == document_id,
                m.resume_revisions.c.profile_id == profile_id,
            ).with_for_update()).first()
            if revision is None:
                raise LookupError("Document is not available to this profile")
        return

    application = connection.execute(select(m.applications.c.id).where(
        m.applications.c.id == application_id,
        m.applications.c.profile_id == profile_id,
    ).with_for_update()).first()
    if application is None:
        raise LookupError("Document is not available to this profile")
    if document_id is not None:
        document = connection.execute(select(m.documents.c.id).where(
            m.documents.c.id == document_id,
            m.documents.c.application_id == application_id,
        )).first()
        if document is None:
            raise LookupError("Document is not available to this profile")


def _next_timestamp(connection, entity_id: str) -> str:
    latest = connection.execute(select(m.audit.c.timestamp).where(
        m.audit.c.entity_id == entity_id,
        m.audit.c.action.in_(ARCHIVE_ACTIONS),
    ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).scalar_one_or_none()
    current = datetime.fromisoformat(now()).astimezone(timezone.utc)
    if latest is not None:
        previous = datetime.fromisoformat(latest)
        if previous.tzinfo is None:
            raise ValueError("Stored archive event timestamp must include a timezone")
        current = max(current, previous.astimezone(timezone.utc) + timedelta(microseconds=1))
    return current.isoformat()


def _payload(profile_id: str, application_id: str | None, document_id: str,
             operation_id: str, payload: dict | None) -> dict:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("Archive event payload must be an object")
    reserved = {"profile_id", "application_id", "document_id", "operation_id"}
    if reserved.intersection(payload):
        raise ValueError("Archive event payload cannot override its scope")
    try:
        copied = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        encoded = json.dumps(copied, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Archive event payload must contain JSON values") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Archive event payload exceeds the supported size")
    return {
        **copied,
        "profile_id": profile_id,
        "application_id": application_id,
        "document_id": document_id,
        "operation_id": operation_id,
    }


def _same_event(row, payload: dict, profile_id: str, application_id: str | None,
                document_id: str, action: str, operation_id: str) -> dict:
    if row["action"] != action or row["payload"] != payload:
        raise ValueError("Archive operation identity was reused with different event data")
    return {
        "id": row["id"],
        "profile_id": profile_id,
        "application_id": application_id,
        "document_id": document_id,
        "operation_id": operation_id,
        "action": action,
        "payload": row["payload"],
        "timestamp": row["timestamp"],
    }


def _validate_action(action: str) -> None:
    if not isinstance(action, str) or action not in ARCHIVE_ACTIONS:
        raise ValueError("Unsupported document archive event action")


def _validate_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a non-empty identifier of at most 128 characters")
