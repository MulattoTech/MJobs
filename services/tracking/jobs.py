"""Append-only job dispositions and private user notes in audit history."""

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert

DISPOSITIONS = {"ACTIVE", "SAVED", "HIDDEN", "NOT_INTERESTED"}
ENTITY_PREFIX = "job-tracking"


def _entity(profile_id: str, job_id: str) -> str:
    return identity(ENTITY_PREFIX, profile_id, job_id)


def read_job_tracking(store, profile_id: str, job_id: str) -> dict:
    store.profile(profile_id)
    store.job(job_id)
    entity = _entity(profile_id, job_id)
    return _tracking_from_rows(_rows(store, [entity])[entity])


def _rows(store, entities):
    if not entities:
        return {}
    with store.engine.connect() as connection:
        rows = list(connection.execute(select(m.audit).where(
            m.audit.c.entity_id.in_(entities),
            m.audit.c.action.in_(("JOB_DISPOSITION", "JOB_NOTE")),
        )).mappings())
    grouped = {entity: [] for entity in entities}
    for row in rows:
        grouped[row["entity_id"]].append(row)
    return grouped


def _tracking_from_rows(rows):
    disposition = "ACTIVE"
    notes = []
    history = []
    rows = sorted(rows, key=lambda row: (
        row["payload"].get("sequence", 0), row["timestamp"], row["id"]))
    for row in rows:
        payload = row["payload"]
        if row["action"] == "JOB_DISPOSITION":
            disposition = payload["disposition"]
            history.append({"type": "disposition", "disposition": disposition,
                            "sequence": payload["sequence"],
                            "timestamp": row["timestamp"]})
        else:
            note = {"id": payload["note_id"], "text": payload["text"],
                    "sequence": payload["sequence"],
                    "timestamp": row["timestamp"]}
            notes.append(note)
            history.append({"type": "note", **note})
    return {"disposition": disposition, "notes": notes, "history": history}


def list_job_tracking(store, profile_id: str, job_ids: list[str]) -> dict:
    entities = {_entity(profile_id, job_id): job_id for job_id in job_ids}
    rows = _rows(store, list(entities))
    return {job_id: {"disposition": _tracking_from_rows(rows[entity])["disposition"]}
            for entity, job_id in entities.items()}


def _append_event(store, profile_id: str, job_id: str, request_id: str,
                  action: str, values: dict) -> dict:
    store.profile(profile_id)
    entity = _entity(profile_id, job_id)
    event_id = identity("job-tracking-event", profile_id, job_id, request_id)
    expected = dict(values)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            # Serialize read/sequence allocation/write before reading the last event.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(select(m.jobs.c.id).where(
            m.jobs.c.id == job_id).with_for_update()).one()
        existing = connection.execute(select(m.audit).where(
            m.audit.c.id == event_id)).mappings().one_or_none()
        if existing is not None:
            if (existing["entity_id"] != entity or existing["action"] != action
                    or any(existing["payload"].get(key) != value
                           for key, value in expected.items())):
                raise ValueError("Tracking request identifier was already used for different content")
            return {"event_id": event_id, "created": False}
        rows = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == entity,
            m.audit.c.action.in_(("JOB_DISPOSITION", "JOB_NOTE")),
        )).mappings()
        sequence = max((row["payload"].get("sequence", 0) for row in rows), default=0) + 1
        payload = {**expected, "sequence": sequence}
        upsert(connection, m.audit, {
            "id": event_id, "entity_id": entity, "action": action,
            "payload": payload, "timestamp": now(),
        })
    return {"event_id": event_id, "created": True}


def set_job_disposition(store, profile_id: str, job_id: str,
                        disposition: str, request_id: str) -> dict:
    if disposition not in DISPOSITIONS:
        raise ValueError("Unsupported job disposition")
    _append_event(store, profile_id, job_id, request_id,
                  "JOB_DISPOSITION", {"disposition": disposition})
    return read_job_tracking(store, profile_id, job_id)


def add_job_note(store, profile_id: str, job_id: str,
                 text: str, request_id: str) -> dict:
    text = text.strip()
    if not text or len(text) > 4000:
        raise ValueError("Job note must contain 1 to 4000 characters")
    note_id = identity("job-tracking-note", profile_id, job_id, request_id)
    _append_event(store, profile_id, job_id, request_id, "JOB_NOTE",
                  {"note_id": note_id, "text": text})
    return read_job_tracking(store, profile_id, job_id)
