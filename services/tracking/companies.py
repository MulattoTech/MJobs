"""Profile-scoped company notes stored as append-only audit events."""

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert

TRACKING_ACTIONS = ("COMPANY_NOTE",)


def _entity(profile_id: str, company_id: str) -> str:
    return identity("company-tracking", profile_id, company_id)


def _require_association(connection, profile_id: str, company_id: str) -> None:
    associated = connection.execute(select(m.audit.c.id).where(
        m.audit.c.entity_id == profile_id,
        m.audit.c.action.in_(("COMPANY_DISCOVERED", "COMPANY_SOURCE_ENROLLED")),
        m.audit.c.payload["company_id"].as_string() == company_id,
    ).limit(1)).first()
    if associated is None:
        raise LookupError("Company is not associated with this profile")


def _tracking_from_rows(rows):
    notes = []
    for row in sorted(rows, key=lambda item: (
            item["payload"].get("sequence", 0), item["timestamp"], item["id"])):
        payload = row["payload"]
        notes.append({"id": payload["note_id"], "text": payload["text"],
                      "sequence": payload.get("sequence", 0),
                      "timestamp": row["timestamp"]})
    return {"notes": notes, "history": [{"type": "note", **note} for note in notes]}


def read_company_tracking(store, profile_id: str, company_id: str) -> dict:
    store.profile(profile_id)
    entity = _entity(profile_id, company_id)
    with store.engine.connect() as connection:
        _require_association(connection, profile_id, company_id)
        rows = list(connection.execute(select(m.audit).where(
            m.audit.c.entity_id == entity,
            m.audit.c.action.in_(TRACKING_ACTIONS),
        )).mappings())
    return _tracking_from_rows(rows)


def add_company_note(store, profile_id: str, company_id: str,
                     text: str, request_id: str) -> dict:
    text = text.strip()
    if not text or len(text) > 4000:
        raise ValueError("Company note must contain 1 to 4000 characters")
    if not request_id or len(request_id) > 64 or request_id != request_id.strip():
        raise ValueError("Tracking request identifier must contain 1 to 64 unpadded characters")
    store.profile(profile_id)
    entity = _entity(profile_id, company_id)
    event_id = identity("company-tracking-event", profile_id, company_id, request_id)
    note_id = identity("company-tracking-note", profile_id, company_id, request_id)
    expected = {"note_id": note_id, "text": text}
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        company = connection.execute(select(m.companies.c.id).where(
            m.companies.c.id == company_id).with_for_update()).one_or_none()
        if company is None:
            raise LookupError("Company is not associated with this profile")
        _require_association(connection, profile_id, company_id)
        existing = connection.execute(select(m.audit).where(
            m.audit.c.id == event_id)).mappings().one_or_none()
        if existing is not None:
            if (existing["entity_id"] != entity or existing["action"] != "COMPANY_NOTE"
                    or any(existing["payload"].get(key) != value
                           for key, value in expected.items())):
                raise ValueError("Tracking request identifier was already used for different content")
        else:
            prior = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == entity,
                m.audit.c.action.in_(TRACKING_ACTIONS),
            )).mappings()
            sequence = max((row["payload"].get("sequence", 0) for row in prior), default=0) + 1
            upsert(connection, m.audit, {
                "id": event_id, "entity_id": entity, "action": "COMPANY_NOTE",
                "payload": {**expected, "sequence": sequence}, "timestamp": now(),
            })
    return read_company_tracking(store, profile_id, company_id)
