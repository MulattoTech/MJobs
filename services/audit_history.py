"""Safe, profile-scoped summaries of append-only audit history."""

from sqlalchemy import select

from packages.persistence import models as m


_PROFILE_EVENTS = {
    "PROFILE_SAVED": ("PROFILE_UPDATED", "Profile details updated"),
    "MATCHES_RECALCULATED": ("MATCH_SCORED", "Job matches recalculated"),
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
    "APPLICATION_ANSWERS_EXPORTED": ("APPLICATION_ANSWERS_EXPORTED", "Reviewed answers exported"),
}


def list_audit_history(store, profile_id: str, limit: int = 100) -> dict:
    """Return a bounded timeline without exposing stored audit payloads."""
    store.profile(profile_id)
    limit = max(1, min(int(limit), 200))
    events = []
    with store.engine.connect() as connection:
        profile_rows = connection.execute(select(
            m.audit.c.id, m.audit.c.action, m.audit.c.timestamp,
        ).where(m.audit.c.entity_id == profile_id,
               m.audit.c.action.in_(_PROFILE_EVENTS)).order_by(
            m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings()
        for row in profile_rows:
            mapped = _PROFILE_EVENTS.get(row["action"])
            if mapped:
                event_type, summary = mapped
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
            documents_by_application = {}
            document_rows = connection.execute(select(
                m.documents.c.application_id, m.documents.c.kind,
            ).where(m.documents.c.application_id.in_(applications))).mappings()
            for document in document_rows:
                documents_by_application.setdefault(document["application_id"], set()).add(document["kind"])
            application_audits = connection.execute(select(
                m.audit.c.id, m.audit.c.entity_id, m.audit.c.action, m.audit.c.timestamp,
            ).where(m.audit.c.entity_id.in_(applications),
                    m.audit.c.action.in_([*_APPLICATION_EVENTS, "DOCUMENTS_PREPARED"])).order_by(
                m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings()
        else:
            application_audits = []
        for row in application_audits:
            application = applications[row["entity_id"]]
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
