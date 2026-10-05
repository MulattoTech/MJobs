"""Manual application pipeline status and notes in append-only audit history."""

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert


PIPELINE_STATUSES = {
    "SAVED", "READY", "APPLIED", "RECRUITER_CONTACT", "INTERVIEW",
    "TECHNICAL_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN",
}
ATTRIBUTION = "USER_REPORTED"
ENTITY_PREFIX = "application-tracking"
TRACKING_ACTIONS = ("APPLICATION_PIPELINE_STATUS", "APPLICATION_NOTE")


def _entity(profile_id: str, application_id: str) -> str:
    return identity(ENTITY_PREFIX, profile_id, application_id)


def _rows(store, entities):
    grouped = {entity: [] for entity in entities}
    if not entities:
        return grouped
    with store.engine.connect() as connection:
        rows = connection.execute(select(m.audit).where(
            m.audit.c.entity_id.in_(entities),
            m.audit.c.action.in_(TRACKING_ACTIONS),
        )).mappings()
        for row in rows:
            grouped[row["entity_id"]].append(row)
    return grouped


def _tracking_from_rows(rows):
    status = None
    history = []
    notes = []
    for row in sorted(rows, key=lambda item: (
            item["payload"].get("sequence", 0), item["timestamp"], item["id"])):
        payload = row["payload"]
        sequence = payload.get("sequence", 0)
        if row["action"] == "APPLICATION_PIPELINE_STATUS":
            status = payload["status"]
            event = {"type": "status", "status": status,
                     "attribution": payload.get("attribution", ATTRIBUTION),
                     "sequence": sequence, "timestamp": row["timestamp"]}
        else:
            note = {"id": payload["note_id"], "text": payload["text"],
                    "sequence": sequence, "timestamp": row["timestamp"]}
            notes.append(note)
            event = {"type": "note", **note}
        history.append(event)
    return {"status": status, "attribution": ATTRIBUTION if status else None,
            "notes": notes, "history": history}


def read_application_tracking(store, profile_id: str, application_id: str) -> dict:
    store.profile(profile_id)
    application = store.application(application_id)
    if application["profile_id"] != profile_id:
        raise NoResultFound("Application is not owned by this profile")
    entity = _entity(profile_id, application_id)
    return _tracking_from_rows(_rows(store, [entity])[entity])


def list_application_tracking(store, profile_id: str, application_ids: list[str]) -> dict:
    store.profile(profile_id)
    entities = {_entity(profile_id, application_id): application_id
                for application_id in application_ids}
    rows = _rows(store, list(entities))
    return {application_id: _tracking_from_rows(rows[entity])
            for entity, application_id in entities.items()}


def _append_event(store, profile_id: str, application_id: str, request_id: str,
                  action: str, values: dict) -> None:
    store.profile(profile_id)
    entity = _entity(profile_id, application_id)
    event_id = identity("application-tracking-event", profile_id,
                        application_id, request_id)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one()
        if application["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        existing = connection.execute(select(m.audit).where(
            m.audit.c.id == event_id)).mappings().one_or_none()
        if existing is not None:
            if (existing["entity_id"] != entity or existing["action"] != action
                    or any(existing["payload"].get(key) != value
                           for key, value in values.items())):
                raise ValueError("Tracking request identifier was already used for different content")
            return
        prior = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == entity,
            m.audit.c.action.in_(TRACKING_ACTIONS),
        )).mappings()
        sequence = max((row["payload"].get("sequence", 0) for row in prior),
                       default=0) + 1
        upsert(connection, m.audit, {
            "id": event_id,
            "entity_id": entity,
            "action": action,
            "payload": {**values, "sequence": sequence},
            "timestamp": now(),
        })


def set_application_pipeline_status(store, profile_id: str, application_id: str,
                                    status: str, request_id: str) -> dict:
    if status not in PIPELINE_STATUSES:
        raise ValueError("Unsupported application pipeline status")
    _append_event(store, profile_id, application_id, request_id,
                  "APPLICATION_PIPELINE_STATUS",
                  {"status": status, "attribution": ATTRIBUTION})
    return read_application_tracking(store, profile_id, application_id)


def add_application_note(store, profile_id: str, application_id: str,
                         text: str, request_id: str) -> dict:
    text = text.strip()
    if not text or len(text) > 4000:
        raise ValueError("Application note must contain 1 to 4000 characters")
    note_id = identity("application-tracking-note", profile_id,
                       application_id, request_id)
    _append_event(store, profile_id, application_id, request_id,
                  "APPLICATION_NOTE", {"note_id": note_id, "text": text})
    return read_application_tracking(store, profile_id, application_id)
