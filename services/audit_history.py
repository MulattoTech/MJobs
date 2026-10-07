"""Safe, profile-scoped summaries of append-only audit history."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.persistence import models as m
from packages.persistence.store import identity, now


_PROFILE_EVENTS = {
    "PROFILE_SAVED": ("PROFILE_UPDATED", "Profile details updated"),
    "MATCHES_RECALCULATED": ("MATCHING_REFRESHED", "Profile matching refreshed"),
    "JOB_DISCOVERED": ("JOB_DISCOVERED", "Job added to this profile's search"),
    "MATCH_SCORED": ("MATCH_SCORED", "Job match scored"),
    "SETTING_CHANGED": ("SETTING_CHANGED", "A profile setting changed"),
}
_APPLICATION_EVENTS = {
    "APPLICATION_QUEUED": ("APPLICATION_QUEUED", "Application preparation queued"),
    "QUEUED": ("APPLICATION_QUEUED", "Application preparation queued"),
    "STARTED": ("APPLICATION_STARTED", "Application preparation started"),
    "FAILED": ("APPLICATION_FAILED", "Application preparation failed"),
    "CONFIRMED": ("APPLICATION_CONFIRMED", "Application outcome confirmed"),
    "APPLICATION_PIPELINE_STATUS": ("APPLICATION_STATUS_REPORTED", "Application status reported by you"),
    "APPLICATION_NOTE": ("APPLICATION_NOTE_ADDED", "Application note added"),
    "EXTERNAL_APPLICATION_REPORTED": ("APPLICATION_REPORTED", "You reported applying outside MJobs"),
    "APPLICATION_ANSWERS_EXPORTED": ("APPLICATION_ANSWERS_EXPORTED", "Reviewed answers exported"),
}


def record_job_discovered(connection, profile_id: str, job_id: str) -> None:
    _append_once(connection, profile_id, "JOB_DISCOVERED", job_id,
                 {"job_id": job_id})


def record_match_scored(connection, profile_id: str, job_id: str,
                        profile_version: str) -> None:
    _append_once(connection, profile_id, "MATCH_SCORED", job_id,
                 {"job_id": job_id, "profile_version": profile_version},
                 profile_version=profile_version)


def _append_once(connection, profile_id: str, action: str, job_id: str,
                 payload: dict, profile_version: str = "") -> None:
    event_id = identity("profile-audit", action, profile_id, job_id, profile_version)
    values = {"id": event_id, "entity_id": profile_id, "action": action,
              "payload": payload, "timestamp": now()}
    dialect = connection.dialect.name
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
        profile_job_ids = {
            (row["payload"] or {}).get("job_id") for row in profile_rows
            if row["action"] in {"JOB_DISCOVERED", "MATCH_SCORED"}
            and isinstance((row["payload"] or {}).get("job_id"), str)
        }
        profile_jobs = {}
        if profile_job_ids:
            profile_jobs = {row["id"]: row for row in connection.execute(select(
                m.jobs.c.id, m.jobs.c.title,
                m.companies.c.name.label("company"),
            ).select_from(m.jobs.join(m.companies,
                                     m.jobs.c.company_id == m.companies.c.id)).where(
                m.jobs.c.id.in_(profile_job_ids),
            )).mappings()}
        for row in profile_rows:
            mapped = _PROFILE_EVENTS.get(row["action"])
            if mapped:
                event_type, summary = mapped
                if row["action"] in {"JOB_DISCOVERED", "MATCH_SCORED"}:
                    job = profile_jobs.get((row["payload"] or {}).get("job_id"))
                    if job is None:
                        continue
                    events.append(_event(row, event_type, summary, job["title"], job["company"]))
                else:
                    events.append(_event(row, event_type, summary, "Profile", ""))

        application_rows = connection.execute(select(
            m.applications.c.id, m.applications.c.target, m.jobs.c.title,
            m.companies.c.name.label("company"),
        ).select_from(m.applications.join(m.jobs, m.applications.c.job_id == m.jobs.c.id)
                      .join(m.companies, m.jobs.c.company_id == m.companies.c.id)).where(
            m.applications.c.profile_id == profile_id,
        ).order_by(m.applications.c.created_at.desc()).limit(200)).mappings().all()
        applications = {row["id"]: row for row in application_rows}
        if applications:
            tracking_entities = {
                identity("application-tracking", profile_id, application_id): application_id
                for application_id in applications
            }
            documents_by_application = {}
            document_rows = connection.execute(select(
                m.documents.c.application_id, m.documents.c.kind,
            ).where(m.documents.c.application_id.in_(applications))).mappings()
            for document in document_rows:
                documents_by_application.setdefault(document["application_id"], set()).add(document["kind"])
            application_audits = connection.execute(select(
                m.audit.c.id, m.audit.c.entity_id, m.audit.c.action, m.audit.c.timestamp,
            ).where(m.audit.c.entity_id.in_([*applications, *tracking_entities]),
                    m.audit.c.action.in_([*_APPLICATION_EVENTS, "DOCUMENTS_PREPARED"])).order_by(
                m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings()
        else:
            tracking_entities = {}
            application_audits = []
        for row in application_audits:
            application_id = row["entity_id"]
            if application_id not in applications:
                application_id = tracking_entities.get(application_id)
            application = applications.get(application_id)
            if application is None:
                continue
            action = row["action"]
            mapped = _APPLICATION_EVENTS.get(action)
            if action == "DOCUMENTS_PREPARED":
                for kind in sorted(documents_by_application.get(row["entity_id"], set())):
                    normalized = str(kind).upper()
                    if "RESUME" in normalized:
                        events.append(_event(row, "RESUME_GENERATED", "Resume prepared", application["title"], application["company"], normalized))
                    elif "COVER_LETTER" in normalized:
                        events.append(_event(row, "COVER_LETTER_GENERATED", "Cover letter prepared", application["title"], application["company"], normalized))
            elif mapped:
                event_type, summary = mapped
                if action == "CONFIRMED" and application["target"] == "MOCK":
                    summary = "Fictional mock application outcome confirmed"
                elif action == "CONFIRMED":
                    # A persisted state alone is not evidence of employer receipt.
                    continue
                events.append(_event(row, event_type, summary, application["title"], application["company"]))

    events.sort(key=lambda event: (event["timestamp"], event["id"]), reverse=True)
    return {"profile_id": profile_id, "events": events[:limit]}


def _event(row, event_type: str, summary: str, subject: str, company: str,
           event_suffix: str = "") -> dict:
    return {
        "id": f"{row['id']}:{event_suffix}" if event_suffix else row["id"],
        "type": event_type,
        "summary": summary,
        "subject": subject,
        "company": company,
        "timestamp": row["timestamp"],
    }
