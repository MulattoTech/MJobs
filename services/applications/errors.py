"""Structured application failures; no classification of free-form messages."""
from sqlalchemy import select

from packages.domain.operation_errors import operation_error_category
from packages.persistence import models as m


class ApplicationValidationError(ValueError):
    code = "FORM_FIELDS_INVALID"


def application_failure_category(store, application):
    """Use the current state/last phase, never an old challenge from prior attempts."""
    category = operation_error_category(application.get("state"))
    if category is not None:
        return category
    trace = application.get("trace")
    last = trace[-1] if isinstance(trace, list) and trace else None
    category = operation_error_category(last)
    if category is not None:
        return category
    if last not in ("QUESTION_PREFLIGHT_FAIL", "QUESTION_PREFLIGHT_UNKNOWN"):
        return None
    try:
        with store.engine.connect() as connection:
            payload = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == application["id"],
                m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
            ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).scalar_one_or_none()
    except Exception:
        # Optional classification must not replace an already-correct blocked
        # application outcome when the read-only enrichment is unavailable.
        return None
    if (not isinstance(payload, dict) or any(payload.get(key) != application.get(key)
            for key in ("profile_version", "document_version", "route_id"))):
        return None
    return operation_error_category(payload.get("reason"))
