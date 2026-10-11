"""Persist value-free review receipts for manually imported email status signals."""
from __future__ import annotations

from hashlib import sha256
import re

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.tracking.applications import TRACKING_ACTIONS, _entity
from services.tracking.email_status import classify_email_status


ATTRIBUTION = "USER_REPORTED_EMAIL"
_PREVIEW_ACTION = "EMAIL_STATUS_PREVIEW"
_RECEIPT_ACTION = "EMAIL_STATUS_REVIEW_RECEIPT"
_CATEGORIES = {
    "CONFIRMATION", "REJECTION", "INTERVIEW_INVITATION", "RECRUITER_OUTREACH",
    "OFFER", "AMBIGUOUS", "UNKNOWN",
}
_SUGGESTIONS = {
    "CONFIRMATION": "APPLIED",
    "REJECTION": "REJECTED",
    "INTERVIEW_INVITATION": "INTERVIEW",
    "RECRUITER_OUTREACH": "RECRUITER_CONTACT",
    "OFFER": "OFFER",
    "AMBIGUOUS": None,
    "UNKNOWN": None,
}
_REASONS = {
    "EXPLICIT_CONFIRMATION", "EXPLICIT_REJECTION", "EXPLICIT_INTERVIEW_INVITATION",
    "EXPLICIT_RECRUITER_OUTREACH", "EXPLICIT_OFFER", "NEGATED_SIGNAL",
    "CONDITIONAL_SIGNAL", "CONFLICTING_SIGNALS", "NO_EXPLICIT_SIGNAL", "INVALID_FORMAT",
    "MIME_LIMIT_EXCEEDED",
}
_STATUS_SET = {"APPLIED", "REJECTED", "INTERVIEW", "RECRUITER_CONTACT", "OFFER"}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FIXED_ERRORS = {
    "EMAIL_STATUS_NOT_APPLICABLE": 409,
    "EMAIL_STATUS_PREVIEW_ALREADY_APPLIED": 409,
    "EMAIL_STATUS_PREVIEW_NOT_FOUND": 404,
    "EMAIL_STATUS_PROFILE_NOT_FOUND": 404,
    "EMAIL_STATUS_REQUEST_CONFLICT": 409,
    "EMAIL_STATUS_STALE_PREVIEW": 409,
    "EMAIL_STATUS_INPUT_INVALID": 422,
}


class EmailStatusError(ValueError):
    """Safe, stable service error intended for the API's public mapping."""

    def __init__(self, code: str):
        self.code = code if code in _FIXED_ERRORS else "EMAIL_STATUS_INPUT_INVALID"
        self.status_code = _FIXED_ERRORS[self.code]
        super().__init__(self.code)


def _check_owned_application(connection, profile_id: str, application_id: str):
    row = connection.execute(select(
        m.applications.c.id, m.applications.c.profile_id,
    ).where(m.applications.c.id == application_id)).mappings().one_or_none()
    if row is None or row["profile_id"] != profile_id:
        raise EmailStatusError("EMAIL_STATUS_PROFILE_NOT_FOUND")
    return row


def _current_tracking_sequence(connection, profile_id: str, application_id: str) -> int:
    rows = connection.execute(select(m.audit.c.payload).where(
        m.audit.c.entity_id == _entity(profile_id, application_id),
        m.audit.c.action.in_(TRACKING_ACTIONS),
    )).mappings()
    return max((row["payload"].get("sequence", 0)
                for row in rows if isinstance(row["payload"], dict)), default=0)


def _safe_classification(content: str, result: object) -> dict:
    if not isinstance(result, dict):
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID")
    digest = sha256(content.encode("utf-8")).hexdigest()
    category = result.get("category")
    reasons = result.get("reason_codes")
    if (result.get("content_sha256") != digest or not isinstance(category, str)
            or category not in _CATEGORIES
            or not isinstance(reasons, list) or len(reasons) > 10
            or any(not isinstance(reason, str) or reason not in _REASONS for reason in reasons)):
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID")
    expected_status = _SUGGESTIONS[category]
    if result.get("suggested_status") != expected_status:
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID")
    return {
        "content_sha256": digest,
        "category": category,
        "suggested_status": expected_status,
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def _preview_result(payload: dict) -> dict:
    return {
        "preview_id": payload["preview_id"],
        "content_sha256": payload["content_sha256"],
        "category": payload["category"],
        "suggested_status": payload["suggested_status"],
        "reason_codes": list(payload["reason_codes"]),
        "expected_tracking_sequence": payload["expected_tracking_sequence"],
        "attribution": ATTRIBUTION,
    }


def preview_email_status(store, profile_id: str, application_id: str,
                         content: str, format: str = "text") -> dict:
    """Classify bounded local content and save only its hash and fixed-code summary."""
    if (not isinstance(content, str) or not isinstance(format, str)
            or format not in {"text", "eml"}):
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID")
    try:
        content_size = len(content.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID") from None
    if content_size > 256 * 1024:
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID")
    try:
        classification = _safe_classification(
            content, classify_email_status(content, format=format))
    except EmailStatusError:
        raise
    except (ValueError, TypeError, UnicodeError):
        # Parser errors are never returned verbatim because they may contain input.
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID") from None

    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        _check_owned_application(connection, profile_id, application_id)
        sequence = _current_tracking_sequence(connection, profile_id, application_id)
        preview_id = identity("email-status-preview", profile_id, application_id,
                              format, classification["content_sha256"], sequence)
        event_id = identity("email-status-preview-event", preview_id)
        event_entity = _entity(profile_id, application_id)
        payload = {
            "preview_id": preview_id,
            **classification,
            "format": format,
            "expected_tracking_sequence": sequence,
            "attribution": ATTRIBUTION,
        }
        existing = connection.execute(select(m.audit).where(
            m.audit.c.id == event_id)).mappings().one_or_none()
        if existing is None:
            upsert(connection, m.audit, {
                "id": event_id,
                "entity_id": event_entity,
                "action": _PREVIEW_ACTION,
                "payload": payload,
                "timestamp": now(),
            })
        elif (existing["entity_id"] != event_entity or existing["action"] != _PREVIEW_ACTION
              or existing["payload"] != payload):
            raise EmailStatusError("EMAIL_STATUS_REQUEST_CONFLICT")
    return _preview_result(payload)


def _receipt_result(payload: dict, *, idempotent: bool) -> dict:
    return {
        "preview_id": payload["preview_id"],
        "applied": True,
        "idempotent": idempotent,
        "status": payload["reported_status"],
        "attribution": ATTRIBUTION,
        "sequence": payload["sequence"],
    }


def apply_email_status(store, profile_id: str, application_id: str, preview_id: str,
                       request_id: str, status: str, expected_tracking_sequence: int,
                       reviewed: bool) -> dict:
    """Atomically append a compatible user-reported tracking event and receipt."""
    if (not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
            or not isinstance(preview_id, str) or not preview_id or len(preview_id) > 128
            or status not in _STATUS_SET or type(expected_tracking_sequence) is not int
            or expected_tracking_sequence < 0 or reviewed is not True):
        raise EmailStatusError("EMAIL_STATUS_INPUT_INVALID")

    tracking_entity = _entity(profile_id, application_id)
    preview_event_id = identity("email-status-preview-event", preview_id)
    receipt_id = identity("email-status-review-receipt", profile_id,
                          application_id, request_id)
    # Prefix the status-event key to avoid colliding with a normal tracking request ID.
    status_event_id = identity("email-status-application-tracking-event", profile_id,
                               application_id, request_id)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id).with_for_update()).mappings().one_or_none()
        if application is None or application["profile_id"] != profile_id:
            raise EmailStatusError("EMAIL_STATUS_PROFILE_NOT_FOUND")

        receipt = connection.execute(select(m.audit).where(
            m.audit.c.id == receipt_id)).mappings().one_or_none()
        preview = connection.execute(select(m.audit).where(
            m.audit.c.id == preview_event_id)).mappings().one_or_none()
        if preview is None or preview["entity_id"] != tracking_entity \
                or preview["action"] != _PREVIEW_ACTION:
            raise EmailStatusError("EMAIL_STATUS_PREVIEW_NOT_FOUND")
        preview_payload = preview["payload"]
        desired_receipt = {
            "preview_id": preview_id,
            "content_sha256": preview_payload["content_sha256"],
            "expected_tracking_sequence": expected_tracking_sequence,
            "reported_status": status,
            "attribution": ATTRIBUTION,
            "request_id": request_id,
        }

        if receipt is not None:
            stored = receipt["payload"]
            comparable_keys = tuple(desired_receipt)
            if (receipt["entity_id"] != tracking_entity
                    or receipt["action"] != _RECEIPT_ACTION
                    or any(stored.get(key) != desired_receipt[key]
                           for key in comparable_keys)):
                raise EmailStatusError("EMAIL_STATUS_REQUEST_CONFLICT")
            return _receipt_result(stored, idempotent=True)

        prior_receipts = connection.execute(select(m.audit.c.id).where(
            m.audit.c.entity_id == tracking_entity,
            m.audit.c.action == _RECEIPT_ACTION,
            m.audit.c.payload["preview_id"].as_string() == preview_id,
        ).limit(1)).scalar_one_or_none()
        if prior_receipts is not None:
            raise EmailStatusError("EMAIL_STATUS_PREVIEW_ALREADY_APPLIED")

        if (preview_payload.get("expected_tracking_sequence") != expected_tracking_sequence
                or _current_tracking_sequence(connection, profile_id, application_id)
                != expected_tracking_sequence):
            raise EmailStatusError("EMAIL_STATUS_STALE_PREVIEW")
        if (preview_payload.get("suggested_status") not in _STATUS_SET
                or preview_payload.get("suggested_status") != status):
            raise EmailStatusError("EMAIL_STATUS_NOT_APPLICABLE")

        prior_events = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == tracking_entity,
            m.audit.c.action.in_(TRACKING_ACTIONS),
        )).mappings()
        sequence = max((row["payload"].get("sequence", 0)
                        for row in prior_events if isinstance(row["payload"], dict)),
                       default=0) + 1
        status_payload = {
            "status": status,
            "attribution": ATTRIBUTION,
            "sequence": sequence,
        }
        receipt_payload = {
            **desired_receipt,
            "decision": "APPLIED",
            "sequence": sequence,
        }
        existing_status = connection.execute(select(m.audit).where(
            m.audit.c.id == status_event_id)).mappings().one_or_none()
        if existing_status is not None:
            raise EmailStatusError("EMAIL_STATUS_REQUEST_CONFLICT")
        upsert(connection, m.audit, {
            "id": status_event_id,
            "entity_id": tracking_entity,
            "action": "APPLICATION_PIPELINE_STATUS",
            "payload": status_payload,
            "timestamp": now(),
        })
        upsert(connection, m.audit, {
            "id": receipt_id,
            "entity_id": tracking_entity,
            "action": _RECEIPT_ACTION,
            "payload": receipt_payload,
            "timestamp": now(),
        })
    return _receipt_result(receipt_payload, idempotent=False)


def list_email_status_history(store, profile_id: str, application_id: str) -> dict:
    """Return bounded-safe review history; email values are never persisted here."""
    tracking_entity = _entity(profile_id, application_id)
    with store.engine.connect() as connection:
        _check_owned_application(connection, profile_id, application_id)
        rows = connection.execute(select(m.audit).where(
            m.audit.c.entity_id == tracking_entity,
            m.audit.c.action.in_((_PREVIEW_ACTION, _RECEIPT_ACTION)),
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(200)).mappings()
        previews = {}
        receipts = {}
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            if row["action"] == _PREVIEW_ACTION:
                previews[payload.get("preview_id")] = (payload, row["timestamp"])
            elif row["action"] == _RECEIPT_ACTION:
                receipts[payload.get("preview_id")] = (payload, row["timestamp"])
    items = []
    for preview_id, (payload, _preview_timestamp) in previews.items():
        receipt_row = receipts.get(preview_id)
        if receipt_row is None:
            # A saved preview is not a reported status. Keep it available for the
            # explicit apply endpoint, but do not label it as history for the user.
            continue
        receipt, receipt_timestamp = receipt_row
        items.append({
            "preview_id": preview_id,
            "content_sha256": payload.get("content_sha256"),
            "category": payload.get("category"),
            "suggested_status": payload.get("suggested_status"),
            "reason_codes": list(payload.get("reason_codes", [])),
            "expected_tracking_sequence": payload.get("expected_tracking_sequence", 0),
            "decision": receipt.get("decision"),
            "reported_status": receipt.get("reported_status"),
            "attribution": ATTRIBUTION,
            "sequence": receipt.get("sequence"),
            "created_at": receipt_timestamp,
        })
    return {"items": items}


__all__ = ["EmailStatusError", "apply_email_status", "list_email_status_history",
           "preview_email_status"]
