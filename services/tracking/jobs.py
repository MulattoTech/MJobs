"""Append-only job dispositions and private user notes in audit history."""
from uuid import uuid4

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now

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
        ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings())
    grouped = {entity: [] for entity in entities}
    for row in rows:
        grouped[row["entity_id"]].append(row)
    return grouped


def _tracking_from_rows(rows):
    disposition = "ACTIVE"
    notes = []
    history = []
    for row in rows:
        payload = row["payload"]
        if row["action"] == "JOB_DISPOSITION":
            disposition = payload["disposition"]
            history.append({"type": "disposition", "disposition": disposition,
                            "timestamp": row["timestamp"]})
        else:
            note = {"id": payload["note_id"], "text": payload["text"],
                    "timestamp": row["timestamp"]}
            notes.append(note)
            history.append({"type": "note", **note})
    return {"disposition": disposition, "notes": notes, "history": history}


def list_job_tracking(store, profile_id: str, job_ids: list[str]) -> dict:
    entities = {_entity(profile_id, job_id): job_id for job_id in job_ids}
    rows = _rows(store, list(entities))
    return {job_id: {"disposition": _tracking_from_rows(rows[entity])["disposition"]}
            for entity, job_id in entities.items()}


def set_job_disposition(store, profile_id: str, job_id: str, disposition: str) -> dict:
    if disposition not in DISPOSITIONS:
        raise ValueError("Unsupported job disposition")
    store.profile(profile_id)
    store.job(job_id)
    with store.engine.begin() as connection:
        connection.execute(m.audit.insert().values(
            id=uuid4().hex, entity_id=_entity(profile_id, job_id),
            action="JOB_DISPOSITION", payload={"disposition": disposition},
            timestamp=now(),
        ))
    return read_job_tracking(store, profile_id, job_id)


def add_job_note(store, profile_id: str, job_id: str, text: str) -> dict:
    text = text.strip()
    if not text or len(text) > 4000:
        raise ValueError("Job note must contain 1 to 4000 characters")
    store.profile(profile_id)
    store.job(job_id)
    with store.engine.begin() as connection:
        timestamp = now()
        connection.execute(m.audit.insert().values(
            id=uuid4().hex, entity_id=_entity(profile_id, job_id),
            action="JOB_NOTE", payload={"note_id": uuid4().hex, "text": text},
            timestamp=timestamp,
        ))
    return read_job_tracking(store, profile_id, job_id)
