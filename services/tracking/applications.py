"""Manual application pipeline status and notes in append-only audit history."""

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.audit_history import snapshot_application_audit_payload


PIPELINE_STATUSES = {
    "SAVED", "READY", "APPLIED", "RECRUITER_CONTACT", "INTERVIEW",
    "TECHNICAL_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN",
}
ATTRIBUTION = "USER_REPORTED"
ENTITY_PREFIX = "application-tracking"
TRACKING_ACTIONS = ("APPLICATION_PIPELINE_STATUS", "APPLICATION_NOTE",
                    "EXTERNAL_APPLICATION_REPORTED")


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
    attribution = None
    history = []
    notes = []
    external_reports = []
    for row in sorted(rows, key=lambda item: (
            item["payload"].get("sequence", 0), item["timestamp"], item["id"])):
        payload = row["payload"]
        sequence = payload.get("sequence", 0)
        if row["action"] == "APPLICATION_PIPELINE_STATUS":
            status = payload["status"]
            attribution = payload.get("attribution", ATTRIBUTION)
            event = {"type": "status", "status": status,
                     "attribution": attribution,
                     "sequence": sequence, "timestamp": row["timestamp"]}
        elif row["action"] == "APPLICATION_NOTE":
            note = {"id": payload["note_id"], "text": payload["text"],
                    "sequence": sequence, "timestamp": row["timestamp"]}
            notes.append(note)
            event = {"type": "note", **note}
        else:
            report = {"source": payload["source"],
                      "applied_at": payload["applied_at"],
                      "request_id": payload["request_id"],
                      "sequence": sequence, "timestamp": row["timestamp"]}
            if payload.get("application_url"):
                report["application_url"] = payload["application_url"]
            external_reports.append(report)
            event = {"type": "external_application", **report}
        history.append(event)
    result = {"status": status, "attribution": attribution,
              "notes": notes, "history": history}
    if external_reports:
        result["external_reports"] = external_reports
    return result


def reserve_external_application_capture(store, profile_id: str,
                                         request_id: str,
                                         content_sha256: str) -> None:
    """Claim a profile-scoped idempotency key before resolving or creating a job."""
    store.profile(profile_id)
    entity = identity("external-application-capture", profile_id)
    reservation_id = identity("external-application-capture-reservation",
                              profile_id, request_id)
    payload = {"request_id": request_id, "content_sha256": content_sha256}
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id).with_for_update()).one()
        existing = connection.execute(select(m.audit).where(
            m.audit.c.id == reservation_id)).mappings().one_or_none()
        if existing is not None:
            if (existing["entity_id"] != entity
                    or existing["action"] != "EXTERNAL_APPLICATION_CAPTURE_RESERVED"
                    or existing["payload"] != payload):
                raise ValueError("External application request identifier was already used for different content")
            return
        connection.execute(m.audit.insert().values(
            id=reservation_id, entity_id=entity,
            action="EXTERNAL_APPLICATION_CAPTURE_RESERVED",
            payload=payload, timestamp=now(),
        ))


def record_external_application_report(store, profile_id: str, application_id: str,
                                       request_id: str, capture_request_id: str,
                                       content_sha256: str, source: str,
                                       applied_at: str, application_url: str | None,
                                       job_id: str, reconciliation: str) -> dict:
    """Append a user-reported external application without changing later state."""
    store.profile(profile_id)
    entity = _entity(profile_id, application_id)
    event_id = identity("application-tracking-event", profile_id,
                        application_id, request_id)
    capture_entity = identity("external-application-capture", profile_id)
    capture_id = identity("external-application-capture-event", profile_id,
                          capture_request_id)
    report_values = {"request_id": capture_request_id,
                     "content_sha256": content_sha256,
                     "application_id": application_id, "job_id": job_id,
                     "reconciliation": reconciliation}
    tracking_values = {"request_id": capture_request_id,
                       "source": source, "applied_at": applied_at,
                       "application_url": application_url}
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id).with_for_update()).one()
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one()
        if application["profile_id"] != profile_id:
            raise NoResultFound("Application is not owned by this profile")
        existing_capture = connection.execute(select(m.audit).where(
            m.audit.c.id == capture_id)).mappings().one_or_none()
        if existing_capture is not None:
            if (existing_capture["entity_id"] != capture_entity
                    or existing_capture["action"] != "EXTERNAL_APPLICATION_CAPTURE"
                    or any(existing_capture["payload"].get(key) != value
                           for key, value in report_values.items())):
                raise ValueError("External application request identifier was already used for different content")
        else:
            existing_report = connection.execute(select(m.audit).where(
                m.audit.c.id == event_id)).mappings().one_or_none()
            if existing_report is not None:
                raise ValueError("External application tracking request identifier is already in use")
            prior = list(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == entity,
                m.audit.c.action.in_(TRACKING_ACTIONS),
            )).mappings())
            sequence = max((row["payload"].get("sequence", 0) for row in prior),
                           default=0) + 1
            upsert(connection, m.audit, {
                "id": capture_id, "entity_id": capture_entity,
                "action": "EXTERNAL_APPLICATION_CAPTURE",
                "payload": report_values, "timestamp": now(),
            })
            upsert(connection, m.audit, {
                "id": event_id, "entity_id": entity,
                "action": "EXTERNAL_APPLICATION_REPORTED",
                "payload": snapshot_application_audit_payload(connection, application_id,
                    "EXTERNAL_APPLICATION_REPORTED", {**tracking_values, "sequence": sequence}),
                "timestamp": now(),
            })
            statuses = [row for row in prior
                        if row["action"] == "APPLICATION_PIPELINE_STATUS"]
            current_status = max(
                statuses,
                key=lambda row: row["payload"].get("sequence", 0),
                default=None,
            )
            if (current_status is None
                    or current_status["payload"].get("status") in {"SAVED", "READY"}):
                status_id = identity("application-tracking-event", profile_id,
                                     application_id, request_id + ":reported-status")
                upsert(connection, m.audit, {
                    "id": status_id, "entity_id": entity,
                    "action": "APPLICATION_PIPELINE_STATUS",
                    "payload": snapshot_application_audit_payload(connection, application_id,
                        "APPLICATION_PIPELINE_STATUS", {"status": "APPLIED",
                                "attribution": "EXTERNAL_USER_REPORTED",
                                "sequence": sequence + 1}),
                    "timestamp": now(),
                })
    return {"created": existing_capture is None,
            "tracking": read_application_tracking(store, profile_id, application_id)}


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
            "payload": snapshot_application_audit_payload(connection, application_id,
                action, {**values, "sequence": sequence}),
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
