"""Read-only, profile-scoped application form rehearsal snapshots.

The service combines only the current local profile snapshot with the latest
durable, value-free form analysis. It never writes application or provider
state and never persists populated values.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from sqlalchemy import select

from packages.persistence import models as m
from services.applications.form_review_providers import normalize_application_route
from services.documents.answers import match_answer_question


_AUDIT_ACTION = "APPLICATION_FORM_REVIEW"
_DOC_SELECTION_ACTION = "APPLICATION_SUPPORTING_DOCUMENTS_SELECTED"
_SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-\[\]]{0,99}\Z")
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SAFE_TYPES = frozenset({
    "text", "email", "tel", "url", "number", "date", "datetime-local",
    "time", "month", "week", "search", "password", "checkbox", "radio",
    "file", "select", "textarea", "other",
})
_STATUSES = frozenset({"ANALYZED", "REVIEW_REQUIRED", "UNKNOWN"})
_SENSITIVE = re.compile(
    r"(?:password|passcode|credential|secret|token|api[_ -]?key|ssn|social[_ -]?security|"
    r"date[_ -]?of[_ -]?birth|birth[_ -]?date|citizenship|authorized|authorization|"
    r"visa|sponsor|race|ethnic|gender|sexual[_ -]?orientation|disabilit|veteran|"
    r"religion|salary|compensation|criminal|background[_ -]?check|captcha|challenge)",
    re.IGNORECASE,
)

# Only these fixed keys have meaning for saved answer reuse. We do not infer
# semantic questions from external labels or page content.
_ANSWER_QUESTIONS = {
    "weekend_availability": "Can you work weekends?",
    "availability_weekends": "Can you work weekends?",
    "can_work_weekends": "Can you work weekends?",
}
_CONTACT_FIELDS = {
    "email": ("email",), "email_address": ("email",),
    "first_name": ("first_name",), "firstname": ("first_name",),
    "given_name": ("first_name",), "middle_name": ("middle_name",),
    "last_name": ("last_name",), "lastname": ("last_name",),
    "family_name": ("last_name",), "preferred_name": ("preferred_name",),
    "phone": ("phones",), "phone_number": ("phones",), "mobile_phone": ("phones",),
    "address": ("address_line1",), "address_line1": ("address_line1",),
    "address_line2": ("address_line2",), "street_address": ("address_line1",),
    "city": ("city",), "state": ("state",), "province": ("state",),
    "postal_code": ("postal_code",), "zip": ("postal_code",),
    "zipcode": ("postal_code",), "country": ("country",),
    "linkedin": ("linkedin",), "linkedin_url": ("linkedin",),
    "github": ("github",), "github_url": ("github",),
    "portfolio": ("portfolio",), "portfolio_url": ("portfolio",),
    "website": ("portfolio",),
}
_DOCUMENT_FIELDS = {
    "resume": ("resume", "Resume"), "cv": ("resume", "Resume"),
    "resume_upload": ("resume", "Resume"), "cv_upload": ("resume", "Resume"),
    "cover_letter": ("cover-letter", "Cover letter"),
    "cover_letter_upload": ("cover-letter", "Cover letter"),
}
_MAX_FIELDS = 100
_MAX_VALUE_LENGTH = 4000


class RehearsalError(ValueError):
    """An error carrying one fixed, non-sensitive public code."""

    def __init__(self, code: str):
        if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
            code = "APPLICATION_REHEARSAL_INVALID"
        self.code = code
        super().__init__(code)


def _profile_and_application(store, profile_id, application_id):
    if (not isinstance(profile_id, str) or not profile_id
            or not isinstance(application_id, str) or not application_id):
        raise RehearsalError("APPLICATION_REHEARSAL_INVALID")
    with store.engine.connect() as connection:
        row = connection.execute(select(
            m.applications.c.id.label("application_id"),
            m.applications.c.profile_id,
            m.applications.c.route_id,
            m.routes.c.provider,
            m.routes.c.url,
        ).select_from(m.applications.join(
            m.routes, m.applications.c.route_id == m.routes.c.id,
        )).where(
            m.applications.c.id == application_id,
            m.applications.c.profile_id == profile_id,
        )).mappings().one_or_none()
    if row is None:
        raise RehearsalError("APPLICATION_NOT_FOUND")
    try:
        profile = store.profile(profile_id)
    except Exception:
        raise RehearsalError("PROFILE_NOT_FOUND") from None
    return dict(row), profile


def _latest_analysis(store, application, profile_id, profile):
    with store.engine.connect() as connection:
        latest = connection.execute(select(m.audit).where(
            m.audit.c.entity_id == application["application_id"],
            m.audit.c.action == _AUDIT_ACTION,
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())
            .limit(1)).mappings().one_or_none()
        latest = dict(latest) if latest is not None else None
    if latest is None:
        raise RehearsalError("FORM_REVIEW_REQUIRED")
    payload = latest.get("payload")
    if (not isinstance(payload, Mapping)
            or payload.get("profile_id") != profile_id
            or payload.get("route_id") != application["route_id"]
            or payload.get("provider") != application["provider"]):
        raise RehearsalError("FORM_REVIEW_STALE")
    _validate_operation_snapshot(store, application, profile_id, profile, payload)
    analysis = payload.get("analysis")
    if (not isinstance(analysis, Mapping)
            or set(analysis) != {"status", "reason", "fields"}
            or not isinstance(analysis.get("status"), str)
            or analysis["status"] not in _STATUSES
            or not isinstance(analysis.get("fields"), list)
            or len(analysis["fields"]) > _MAX_FIELDS):
        raise RehearsalError("FORM_ANALYSIS_INVALID")
    reason = analysis.get("reason")
    if reason is not None and (not isinstance(reason, str) or not _SAFE_CODE.fullmatch(reason)):
        raise RehearsalError("FORM_ANALYSIS_INVALID")
    fields = []
    for field in analysis["fields"]:
        if not isinstance(field, Mapping) or set(field) != {
            "id", "name", "type", "required", "review_reason",
        }:
            raise RehearsalError("FORM_ANALYSIS_INVALID")
        for key in ("id", "name"):
            value = field[key]
            if value is not None and (
                    not isinstance(value, str) or not _SAFE_ID.fullmatch(value)):
                raise RehearsalError("FORM_ANALYSIS_INVALID")
        if (not isinstance(field["type"], str) or field["type"] not in _SAFE_TYPES
                or type(field["required"]) is not bool):
            raise RehearsalError("FORM_ANALYSIS_INVALID")
        review_reason = field["review_reason"]
        if review_reason is not None and (
                not isinstance(review_reason, str) or not _SAFE_CODE.fullmatch(review_reason)):
            raise RehearsalError("FORM_ANALYSIS_INVALID")
        fields.append({
            "id": field["id"], "name": field["name"], "type": field["type"],
            "required": field["required"], "review_reason": review_reason,
        })
    return {"status": analysis["status"], "reason": reason, "fields": fields}, payload


def _validate_operation_snapshot(store, application, profile_id, profile, payload):
    """Bind real worker receipts to the route and profile version they inspected.

    Synthetic consumers may persist only a sanitized audit receipt, so a
    missing operation row is tolerated. When the durable worker row exists,
    every frozen input and the worker's content fingerprint must agree.
    """
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise RehearsalError("FORM_ANALYSIS_INVALID")
    with store.engine.connect() as connection:
        operation = connection.execute(select(
            m.operations.c.kind, m.operations.c.entity_id, m.operations.c.params,
        ).where(m.operations.c.id == operation_id).limit(1)).mappings().one_or_none()
    if operation is None:
        return
    params = operation["params"]
    normalized_route = normalize_application_route(
        application["provider"], application["url"],
    )
    if normalized_route is None:
        raise RehearsalError("FORM_REVIEW_STALE")
    expected = {
        "profile_id": profile_id,
        "application_id": application["application_id"],
        "route_id": application["route_id"],
        "route_url": normalized_route["url"],
        "provider": application["provider"],
        "profile_version": str(profile["version"]),
        "request_id": payload.get("request_id"),
        "input_fingerprint": payload.get("input_fingerprint"),
    }
    if (operation["kind"] != _AUDIT_ACTION
            or operation["entity_id"] != application["application_id"]
            or not isinstance(params, Mapping)
            or any(params.get(key) != value for key, value in expected.items())):
        raise RehearsalError("FORM_REVIEW_STALE")
    frozen = {key: params[key] for key in (
        "profile_id", "application_id", "route_id", "route_url", "provider",
        "profile_version",
    )}
    actual_fingerprint = hashlib.sha256(json.dumps(
        frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    if params.get("input_fingerprint") != actual_fingerprint:
        raise RehearsalError("FORM_REVIEW_STALE")


def _contact_value(profile, key, field_type):
    if field_type not in {"text", "email", "tel", "url"}:
        return None
    contact = profile.get("contact")
    if not isinstance(contact, Mapping):
        contact = {}
    contact_key = _CONTACT_FIELDS.get(key)
    if contact_key is None:
        if key in {"full_name", "fullname"}:
            if field_type != "text":
                return None
            given = contact.get("first_name")
            family = contact.get("last_name")
            composed = " ".join(part.strip() for part in (given, family)
                                if isinstance(part, str) and part.strip())
            value = composed or profile.get("name")
        else:
            value = None
    elif contact_key == ("email",):
        if field_type not in {"email", "text"}:
            return None
        value = profile.get("email")
    elif contact_key == ("phones",):
        if field_type not in {"tel", "text"}:
            return None
        phones = contact.get("phones")
        value = phones[0] if isinstance(phones, list) and phones else None
    else:
        if contact_key[0] in {"linkedin", "github", "portfolio"}:
            if field_type not in {"url", "text"}:
                return None
        elif field_type != "text":
            return None
        value = contact.get(contact_key[0])
    if (not isinstance(value, str) or not value.strip()
            or len(value) > _MAX_VALUE_LENGTH):
        return None
    return value


def _approved_answer(profile, key):
    question = _ANSWER_QUESTIONS.get(key)
    if question is None:
        return None, "UNKNOWN_FIELD"
    candidates = [answer for answer in profile.get("answers", [])
                  if isinstance(answer, Mapping)
                  and answer.get("question") == question]
    if not candidates:
        return None, "ANSWER_NOT_VERIFIED"
    answer = candidates[-1]
    if (answer.get("verified") is not True or answer.get("user_approved") is not True
            or type(answer.get("confidence")) is not int
            or answer["confidence"] < 80):
        return None, "ANSWER_NOT_VERIFIED"
    if match_answer_question(question, answer.get("question"), minimum_confidence=1.0) is None:
        return None, "ANSWER_NOT_VERIFIED"
    value = answer.get("text")
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_VALUE_LENGTH:
        return None, "ANSWER_NOT_VERIFIED"
    return value, None


def _documents(store, application, profile_version):
    """Return generated artifact metadata only; never expose stored paths."""
    with store.engine.connect() as connection:
        latest_by_kind = {}
        for kind in ("resume", "cover-letter"):
            row = connection.execute(select(
                m.documents.c.kind, m.documents.c.version,
                m.documents.c.files, m.documents.c.sha256,
            ).where(
                m.documents.c.application_id == application["application_id"],
                m.documents.c.profile_version == profile_version,
                m.documents.c.kind == kind,
            ).order_by(m.documents.c.created_at.desc(), m.documents.c.version.desc())
                .limit(1)).mappings().one_or_none()
            if (row is not None and isinstance(row["files"], Mapping) and row["files"]):
                latest_by_kind[kind] = dict(row)
        selected_events = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == application["application_id"],
            m.audit.c.action == _DOC_SELECTION_ACTION,
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())
            .limit(1)).mappings().one_or_none()
        selected_payload = selected_events["payload"] if selected_events else None
    selected = []
    if (isinstance(selected_payload, Mapping)
            and selected_payload.get("profile_id") == application.get("profile_id")
            and selected_payload.get("application_id") == application["application_id"]):
        items = selected_payload.get("items", [])
        if isinstance(items, list):
            for item in items[:100]:
                if isinstance(item, Mapping):
                    revision_id = item.get("revision_id")
                    if isinstance(revision_id, str) and _SAFE_ID.fullmatch(revision_id):
                        digest = item.get("sha256")
                        selected.append({
                            "revision_id": revision_id,
                            "document_kind": item.get("document_kind"),
                            "sha256": digest if isinstance(digest, str)
                            and re.fullmatch(r"[a-f0-9]{64}", digest) else None,
                        })
    return latest_by_kind, selected


def _safe_field_label(field):
    for value in (field.get("name"), field.get("id")):
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            return value
    return "Unidentified field"


def _document_value(key, docs):
    match = _DOCUMENT_FIELDS.get(key)
    if match is None:
        return None
    kind, label = match
    record = docs.get(kind)
    if record is None:
        return None
    return f"{label} document v{record['version']}"


def _fingerprint(profile_version, application, analysis, docs, selected):
    safe_docs = [
        {"kind": kind, "version": row["version"],
         "sha256": sorted(value for value in (row.get("sha256") or {}).values()
                          if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))}
        for kind, row in sorted(docs.items())
    ]
    payload = {
        "profile_version": profile_version,
        "route_id": application["route_id"],
        "provider": application["provider"],
        "route_url": application["url"],
        "analysis": analysis,
        "documents": safe_docs,
        "selected_documents": selected,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_rehearsal(store, profile_id, application_id) -> dict:
    """Build a bounded profile-only preview from latest durable form metadata.

    Values appear only in this return value for the selected local profile; no
    rehearsal values are saved in the database, audit trail, or remote provider.
    """
    application, profile = _profile_and_application(store, profile_id, application_id)
    analysis, _receipt = _latest_analysis(store, application, profile_id, profile)
    blockers: list[str] = []
    if analysis["status"] != "ANALYZED":
        blockers.append(analysis["reason"] or "FORM_ANALYSIS_UNKNOWN")
    elif not analysis["fields"]:
        blockers.append("FORM_HAS_NO_REVIEWABLE_FIELDS")
    docs, selected = _documents(store, application, profile["version"])
    preview_fields = []
    for field in analysis["fields"]:
        key = field["name"] or field["id"] or "unidentified_field"
        label = _safe_field_label(field)
        value = None
        source = "UNRESOLVED"
        reason_code = None

        if field["review_reason"]:
            reason_code = "FORM_FIELD_REVIEW_REQUIRED"
        elif (field["type"] == "password"
              or _SENSITIVE.search(key)
              or (field["id"] and _SENSITIVE.search(field["id"]))):
            reason_code = "SENSITIVE_FIELD"
        elif field["type"] == "file":
            value = _document_value(key, docs)
            if value is None:
                reason_code = "DOCUMENT_NOT_SELECTED"
            else:
                source = "DOCUMENT"
        else:
            value = _contact_value(profile, key, field["type"])
            if value is not None:
                source = "CONTACT"
            else:
                value, reason_code = _approved_answer(profile, key)
                if value is not None:
                    source = "VERIFIED_ANSWER"

        preview_fields.append({
            "key": key, "label": label, "type": field["type"],
            "required": field["required"], "value": value,
            "source": source, "reason_code": reason_code,
        })
        if source == "UNRESOLVED":
            blockers.append("FORM_FIELD_REVIEW_REQUIRED")
            if field["required"]:
                blockers.append("REQUIRED_FIELD_UNRESOLVED")

    blockers = list(dict.fromkeys(blockers))
    fingerprint = _fingerprint(profile["version"], application, analysis, docs, selected)
    ready = not blockers and all(
        not field["required"] or field["source"] != "UNRESOLVED"
        for field in preview_fields
    )
    return {
        "profile_id": profile_id,
        "application_id": application_id,
        "profile_version": profile["version"],
        "route_id": application["route_id"],
        "fingerprint": fingerprint,
        "fields": preview_fields,
        "blockers": blockers,
        "ready": ready,
        "submission_enabled": False,
    }
