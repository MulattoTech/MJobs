"""Safe, profile-scoped summaries of append-only audit history."""

import re

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql import Executable

from packages.persistence import models as m
from packages.persistence.store import identity, now

_PROFILE_EVENTS = {
    "PROFILE_SAVED": ("PROFILE_UPDATED", "Profile details updated"),
    "MATCHES_RECALCULATED": ("MATCHING_REFRESHED", "Profile matching refreshed"),
    "JOB_DISCOVERED": ("JOB_DISCOVERED", "Job added to this profile's search"),
    "JOB_MERGED": ("JOB_MERGED", "New source posting attached to an existing job"),
    "MATCH_SCORED": ("MATCH_SCORED", "Job match scored"),
    "SETTING_CHANGED": ("SETTING_CHANGED", "A profile setting changed"),
    "APPLICATION_QUEUED": ("APPLICATION_QUEUED", "Application preparation queued"),
    "APPLICATION_STARTED": ("APPLICATION_STARTED", "Application preparation started"),
    "APPLICATION_FAILED": ("APPLICATION_FAILED", "Application preparation failed"),
    "COMPANY_DISCOVERED": ("COMPANY_DISCOVERED", "Company discovered for this profile"),
    "COMPANY_SOURCE_ENROLLED": ("COMPANY_SOURCE_ENROLLED", "Company job source enrolled"),
}
_APPLICATION_PREPARATION_EVENTS = {
    "APPLICATION_QUEUED", "APPLICATION_STARTED", "APPLICATION_FAILED",
}
_COMPANY_EVENTS = {"COMPANY_DISCOVERED", "COMPANY_SOURCE_ENROLLED"}
_DOCUMENT_EVENTS = {
    "resume": ("RESUME_GENERATED", "Resume prepared", "RESUME"),
    "cover-letter": ("COVER_LETTER_GENERATED", "Cover letter prepared", "COVER_LETTER"),
}
# Labels are persisted external/user input. Withhold the complete label when
# it contains a recognized credential marker or URL, rather than guessing how
# far a quoted/multiline secret extends. Unmarked arbitrary secrets are outside
# this bounded filter's detection contract.
_PRIVATE_LABEL = re.compile(
    r"(?<![A-Za-z0-9])(?:api[-_ ]?key|access[-_ ]?key|private[-_ ]?key|client[-_ ]?secret|"
    r"access[-_ ]?token|refresh[-_ ]?token|password|passcode|"
    r"secret|credential|token|session|cookie|authorization)(?:[-_ ](?:id|value))?[\"']?\s*[:=]|"
    r"\b(?:Bearer|Basic)\s+\S+|https?://",
    re.IGNORECASE,
)
_APPLICATION_EVENTS = {
    "APPLICATION_SUBMITTED": ("APPLICATION_SUBMITTED", "Application submitted to the local mock ATS"),
    "APPLICATION_QUEUED": ("APPLICATION_QUEUED", "Application preparation queued"),
    "QUEUED": ("APPLICATION_QUEUED", "Application preparation queued"),
    "STARTED": ("APPLICATION_STARTED", "Application preparation started"),
    "FAILED": ("APPLICATION_FAILED", "Application preparation failed"),
    "CONFIRMED": ("APPLICATION_CONFIRMED", "Application outcome confirmed"),
    "APPLICATION_PIPELINE_STATUS": ("APPLICATION_STATUS_REPORTED", "Application status reported by you"),
    "APPLICATION_NOTE": ("APPLICATION_NOTE_ADDED", "Application note added"),
    "EXTERNAL_APPLICATION_REPORTED": ("APPLICATION_REPORTED", "You reported applying outside MJobs"),
    "APPLICATION_ANSWERS_EXPORTED": ("APPLICATION_ANSWERS_EXPORTED", "Reviewed answers exported"),
    "APPLICATION_CONFIRMATION_CHECKED_V1": (
        "APPLICATION_CONFIRMATION_CHECKED", "Application confirmation checked",
    ),
}

_CONFIRMATION_CHECK_SUMMARIES = {
    "CONFIRMED": "Confirmation check found a confirmed application",
    "NO_VERIFIED_RECEIPT": "Confirmation check found no verified receipt",
    "CHECK_UNAVAILABLE": "Confirmation check was unavailable",
}


def record_mock_application_submitted(store, application_id: str) -> None:
    """Record a completed local mock submit click once, without implying receipt."""
    with store.engine.begin() as connection:
        application = connection.execute(select(
            m.applications.c.id, m.applications.c.profile_id, m.applications.c.target,
        ).where(m.applications.c.id == application_id).with_for_update()).mappings().one()
        if application["target"] != "MOCK":
            raise ValueError("Submission history is restricted to the local mock ATS")
        action = "APPLICATION_SUBMITTED"
        event_id = identity("application-audit", application["profile_id"],
                            application_id, action)
        values = {"id": event_id, "entity_id": application_id,
                  "action": action, "payload": snapshot_application_audit_payload(
                      connection, application_id, action, {}), "timestamp": now()}
        dialect = connection.dialect.name
        statement: Executable
        if dialect == "sqlite":
            statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
                index_elements=[m.audit.c.id])
        elif dialect == "postgresql":
            statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
                index_elements=[m.audit.c.id])
        else:
            if connection.execute(select(m.audit.c.id).where(
                    m.audit.c.id == event_id)).first() is not None:
                return
            statement = m.audit.insert().values(**values)
        connection.execute(statement)


def record_job_discovered(connection, profile_id: str, job_id: str) -> None:
    _append_once(connection, profile_id, "JOB_DISCOVERED", job_id,
                 {"job_id": job_id})


def record_job_merged(connection, profile_id: str, job_id: str, posting_id: str) -> None:
    """Record a newly attached source posting without exposing its source payload."""
    _append_profile_event(connection, "JOB_MERGED", profile_id, posting_id,
                          {"job_id": job_id})


def record_match_scored(connection, profile_id: str, job_id: str,
                        profile_version: str) -> None:
    _append_once(connection, profile_id, "MATCH_SCORED", job_id,
                 {"job_id": job_id, "profile_version": profile_version},
                 profile_version=profile_version)


def record_application_preparation_event(connection, action: str, profile_id: str,
                                         job_id: str, operation_id: str,
                                         attempt: int = 0) -> None:
    """Append an idempotent, profile-owned preparation lifecycle summary."""
    if action not in _APPLICATION_PREPARATION_EVENTS:
        raise ValueError("Unsupported application preparation audit action")
    event_id = identity("profile-audit", action, profile_id, job_id,
                        operation_id, attempt)
    values = {"id": event_id, "entity_id": profile_id, "action": action,
              "payload": _snapshot_payload(connection, action,
                  {"job_id": job_id, "operation_id": operation_id, "attempt": attempt}),
              "timestamp": now()}
    dialect = connection.dialect.name
    statement: Executable
    if dialect == "sqlite":
        statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    elif dialect == "postgresql":
        statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    else:
        if connection.execute(select(m.audit.c.id).where(
                m.audit.c.id == event_id)).first() is not None:
            return
        statement = m.audit.insert().values(**values)
    connection.execute(statement)


def record_company_discovered(connection, profile_id: str, company_id: str) -> None:
    _append_profile_event(connection, "COMPANY_DISCOVERED", profile_id,
                          company_id, {"company_id": company_id})


def record_company_source_enrolled(connection, profile_id: str, company_id: str,
                                   company_source_id: str) -> None:
    _append_profile_event(connection, "COMPANY_SOURCE_ENROLLED", profile_id,
                          company_source_id, {"company_id": company_id,
                                              "company_source_id": company_source_id})


def _append_profile_event(connection, action: str, profile_id: str,
                          stable_subject_id: str, payload: dict) -> None:
    event_id = identity("profile-audit", action, profile_id, stable_subject_id)
    values = {"id": event_id, "entity_id": profile_id, "action": action,
              "payload": _snapshot_payload(connection, action, payload), "timestamp": now()}
    dialect = connection.dialect.name
    statement: Executable
    if dialect == "sqlite":
        statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    elif dialect == "postgresql":
        statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    else:
        if connection.execute(select(m.audit.c.id).where(
                m.audit.c.id == event_id)).first() is not None:
            return
        statement = m.audit.insert().values(**values)
    connection.execute(statement)


def _append_once(connection, profile_id: str, action: str, job_id: str,
                 payload: dict, profile_version: str = "") -> None:
    event_id = identity("profile-audit", action, profile_id, job_id, profile_version)
    values = {"id": event_id, "entity_id": profile_id, "action": action,
              "payload": _snapshot_payload(connection, action, payload), "timestamp": now()}
    dialect = connection.dialect.name
    statement: Executable
    if dialect == "sqlite":
        statement = sqlite_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    elif dialect == "postgresql":
        statement = postgres_insert(m.audit).values(**values).on_conflict_do_nothing(
            index_elements=[m.audit.c.id])
    else:
        if connection.execute(select(m.audit.c.id).where(
                m.audit.c.id == event_id)).first() is not None:
            return
        statement = m.audit.insert().values(**values)
    connection.execute(statement)


def _snapshot_payload(connection, action: str, payload: dict) -> dict:
    """Capture only safe labels in the same transaction as their audit event."""
    if action in _COMPANY_EVENTS:
        subject = connection.execute(select(m.companies.c.name).where(
            m.companies.c.id == payload["company_id"])).scalar_one_or_none()
        company: str | None = ""
    elif action in {"JOB_DISCOVERED", "JOB_MERGED", "MATCH_SCORED",
                    *_APPLICATION_PREPARATION_EVENTS}:
        job = connection.execute(select(m.jobs.c.title, m.companies.c.name).select_from(
            m.jobs.join(m.companies, m.jobs.c.company_id == m.companies.c.id)).where(
                m.jobs.c.id == payload["job_id"])).first()
        subject, company = job if job else (None, None)
    else:
        return payload
    return {**payload, "subject_snapshot": {
        "subject": _display_label(subject, "Details withheld"),
        "company": _display_label(company, ""),
    }}


def _historical_labels(payload) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "Details withheld", ""
    if "subject_snapshot" not in payload:
        # Current entity names cannot establish a historical event's labels.
        return "Historical details not recorded", ""
    snapshot = payload["subject_snapshot"]
    if (not isinstance(snapshot, dict) or set(snapshot) != {"subject", "company"}
            or any(not isinstance(value, str) or len(value) > 500
                   for value in snapshot.values())):
        return "Details withheld", ""
    return (_display_label(snapshot["subject"], "Details withheld"),
            _display_label(snapshot["company"], ""))


def snapshot_application_audit_payload(connection, application_id: str,
                                       action: str, payload: dict) -> dict:
    """Capture safe display labels and ownership in the event's transaction."""
    if action not in {*_APPLICATION_EVENTS, "DOCUMENTS_PREPARED"}:
        return payload
    application = connection.execute(select(m.applications).where(
        m.applications.c.id == application_id)).mappings().one()
    labels = _snapshot_payload(connection, "JOB_DISCOVERED",
                               {"job_id": application["job_id"]})["subject_snapshot"]
    return {**payload, "subject_snapshot": labels, "application_context": {
        "profile_id": application["profile_id"],
        "application_id": application_id,
        "target": application["target"],
    }}


def _application_context(row, profile_id, applications, tracking_entities, owners):
    payload = row["payload"]
    if isinstance(payload, dict) and "application_context" in payload:
        context = payload["application_context"]
        if (not isinstance(context, dict)
                or set(context) != {"profile_id", "application_id", "target"}
                or any(not isinstance(v, str) or not v or len(v) > 128
                       for v in context.values())
                or context["profile_id"] != profile_id
                or context["target"] not in {"MOCK", "REAL", "EXTERNAL_REPORTED"}):
            return None
        application_id = context["application_id"]
        if row["entity_id"] not in {application_id, identity(
                "application-tracking", profile_id, application_id)}:
            return None
        # An extant application also proves ownership. Reject contradictory
        # stored context rather than reassigning history to another profile.
        if application_id in owners and owners[application_id] != profile_id:
            return None
        return context
    # Old rows have no retained owner: use current ownership only to find them,
    # never to infer the labels at the event timestamp.
    application_id = tracking_entities.get(row["entity_id"], row["entity_id"])
    return applications.get(application_id)


def list_audit_history(store, profile_id: str, limit: int = 100) -> dict:
    """Return a bounded timeline without exposing stored audit payloads."""
    store.profile(profile_id)
    limit = max(1, min(int(limit), 200))
    events = []
    with store.engine.connect() as connection:
        profile_rows = list(connection.execute(select(
            m.audit.c.id, m.audit.c.action, m.audit.c.timestamp, m.audit.c.payload,
        ).where(m.audit.c.entity_id == profile_id,
               m.audit.c.action.in_(_PROFILE_EVENTS)).order_by(
            m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings())
        for row in profile_rows:
            event_type, summary = _PROFILE_EVENTS[row["action"]]
            if row["action"] in {"JOB_DISCOVERED", "JOB_MERGED", "MATCH_SCORED",
                                  *_APPLICATION_PREPARATION_EVENTS, *_COMPANY_EVENTS}:
                subject, company = _historical_labels(row["payload"])
            else:
                subject, company = "Profile", ""
            events.append(_event(row, event_type, summary, subject, company))

        application_rows = connection.execute(select(
            m.applications.c.id, m.applications.c.target,
        ).where(
            m.applications.c.profile_id == profile_id,
        ).order_by(m.applications.c.created_at.desc()).limit(200)).mappings().all()
        applications = {row["id"]: row for row in application_rows}
        tracking_entities = {
            identity("application-tracking", profile_id, application_id): application_id
            for application_id in applications
        }
        application_audits = connection.execute(select(
            m.audit.c.id, m.audit.c.entity_id, m.audit.c.action, m.audit.c.timestamp,
            m.audit.c.payload,
        ).where(or_(m.audit.c.entity_id.in_([*applications, *tracking_entities]),
                    m.audit.c.payload["application_context"]["profile_id"].as_string() == profile_id),
                m.audit.c.action.in_([*_APPLICATION_EVENTS, "DOCUMENTS_PREPARED"])).order_by(
            m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings().all()
        context_ids = {row["payload"]["application_context"].get("application_id")
                       for row in application_audits if isinstance(row["payload"], dict)
                       and isinstance(row["payload"].get("application_context"), dict)
                       and isinstance(row["payload"]["application_context"].get("application_id"), str)}
        owners = dict(connection.execute(select(m.applications.c.id, m.applications.c.profile_id).where(
            m.applications.c.id.in_(context_ids))).all()) if context_ids else {}
        for row in application_audits:
            application = _application_context(row, profile_id, applications, tracking_entities, owners)
            if application is None:
                continue
            action = row["action"]
            subject, company = _historical_labels(row["payload"])
            mapped = _APPLICATION_EVENTS.get(action)
            if action == "DOCUMENTS_PREPARED":
                payload = row["payload"]
                kinds = payload.get("document_kinds") if isinstance(payload, dict) else None
                if (isinstance(kinds, list) and 0 < len(kinds) <= len(_DOCUMENT_EVENTS)
                        and all(isinstance(kind, str) and kind in _DOCUMENT_EVENTS for kind in kinds)):
                    for kind in sorted(set(kinds)):
                        event_type, summary, suffix = _DOCUMENT_EVENTS[kind]
                        events.append(_event(row, event_type, summary, subject, company, suffix))
                else:
                    # Older rows did not record kinds. Current artifacts cannot
                    # establish what was generated at a historical timestamp.
                    detail = ("not recorded" if isinstance(payload, dict)
                              and "document_kinds" not in payload else "unavailable")
                    events.append(_event(row, "DOCUMENTS_PREPARED",
                                         f"Documents prepared (document types {detail})",
                                         subject, company))
            elif mapped:
                event_type, summary = mapped
                if action == "CONFIRMED" and application["target"] == "MOCK":
                    summary = "Fictional mock application outcome confirmed"
                elif action == "CONFIRMED":
                    # A persisted state alone is not evidence of employer receipt.
                    continue
                elif action == "APPLICATION_CONFIRMATION_CHECKED_V1":
                    payload = row["payload"]
                    result = payload.get("result") if isinstance(payload, dict) else None
                    status = result.get("status") if isinstance(result, dict) else None
                    summary = _CONFIRMATION_CHECK_SUMMARIES.get(status, summary)
                events.append(_event(row, event_type, summary, subject, company))

    events.sort(key=lambda event: (event["timestamp"], event["id"]), reverse=True)
    return {"profile_id": profile_id, "events": events[:limit]}


def _event(row, event_type: str, summary: str, subject: str, company: str,
           event_suffix: str = "") -> dict:
    return {
        "id": f"{row['id']}:{event_suffix}" if event_suffix else row["id"],
        "type": event_type,
        "summary": summary,
        "subject": _display_label(subject, "Details withheld"),
        "company": _display_label(company, ""),
        "timestamp": row["timestamp"],
    }


def _display_label(value, fallback: str) -> str:
    if not isinstance(value, str) or len(value) > 500 or _PRIVATE_LABEL.search(value):
        return fallback
    return value
