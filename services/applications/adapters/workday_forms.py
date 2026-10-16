"""Strict, metadata-only analyzer for synthetic Workday form descriptions.

This module does not inspect a page or accept applicant answers. It consumes a
versioned metadata envelope supplied by a caller and fails closed whenever the
envelope or a field's semantics are not understood.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from services.applications.form_analysis import analyze_controls


_FORM_KEYS = {"provider", "schema_version", "form_id", "revision", "fields"}
_FIELD_KEYS = {
    "id", "label", "type", "required", "conditional", "conditional_on",
    "repeatable", "repeatable_group",
}
_REQUIRED_FIELD_KEYS = {"id", "label", "type", "required", "conditional", "repeatable"}
_FORBIDDEN_KEYS = {
    "value", "current_value", "selected_value", "checked", "answer", "answers",
    "default_value", "defaultvalue", "input_value", "text_content",
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SUPPORTED_TYPES = {
    "text", "email", "tel", "url", "number", "date", "select", "textarea",
    "checkbox", "radio", "file",
}
WORKDAY_FORM_CAPABILITIES = {
    "metadata_analysis": True,
    "human_review_for_uncertainty": True,
    "page_interaction": False,
    "applicant_value_reading": False,
    "autofill": False,
    "document_upload": False,
    "submission": False,
}


def _unknown(reason: str) -> dict:
    return {"provider": "workday", "status": "UNKNOWN", "reason": reason, "fields": []}


def _normalized_text(value, *, required: bool) -> str | None:
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("INVALID_FIELD_METADATA")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError("INVALID_FIELD_METADATA")
    return value or None


def _has_forbidden_keys(value) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(str(key).casefold() in _FORBIDDEN_KEYS for key in value)


def _sensitivity(field_id: str, label: str, field_type: str) -> str:
    tag = "select" if field_type == "select" else "textarea" if field_type == "textarea" else "input"
    result = analyze_controls([{
        "tag": tag,
        "id": field_id,
        "label": label,
        "input_type": field_type,
    }])
    if result.get("status") != "PASS" or len(result.get("fields", [])) != 1:
        return "UNKNOWN"
    return result["fields"][0].get("sensitivity", "UNKNOWN")


def analyze_workday_form(metadata: Mapping) -> dict:
    """Normalize a strict Workday metadata envelope without reading answer data.

    ``metadata`` must contain ``provider``, schema version 1, stable form and
    revision identifiers, and a list of field metadata. Every field must state
    its required, conditional, and repeatable flags explicitly. Conditional and
    repeated fields are described, but always require human review.
    """
    if not isinstance(metadata, Mapping):
        return _unknown("INVALID_FORM_METADATA")
    if _has_forbidden_keys(metadata):
        return _unknown("FORBIDDEN_VALUE_METADATA")
    if set(metadata) != _FORM_KEYS:
        return _unknown("UNSUPPORTED_FORM_METADATA")
    if metadata.get("provider") != "workday" or metadata.get("schema_version") != 1:
        return _unknown("UNSUPPORTED_FORM_SCHEMA")

    form_id = metadata.get("form_id")
    revision = metadata.get("revision")
    if (not isinstance(form_id, str) or not _IDENTIFIER.fullmatch(form_id)
            or not isinstance(revision, str) or not _SAFE_REVISION.fullmatch(revision)):
        return _unknown("INVALID_FORM_IDENTITY")
    raw_fields = metadata.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > 100:
        return _unknown("INVALID_FIELD_LIST")

    parsed = []
    identities = set()
    review_reasons = set()
    try:
        for raw in raw_fields:
            if not isinstance(raw, Mapping):
                raise ValueError("INVALID_FIELD_METADATA")
            if _has_forbidden_keys(raw):
                raise ValueError("FORBIDDEN_VALUE_METADATA")
            if not _REQUIRED_FIELD_KEYS.issubset(raw) or not set(raw).issubset(_FIELD_KEYS):
                raise ValueError("UNSUPPORTED_FIELD_METADATA")

            field_id = _normalized_text(raw.get("id"), required=True)
            label = _normalized_text(raw.get("label"), required=True)
            field_type = _normalized_text(raw.get("type"), required=True)
            if not _IDENTIFIER.fullmatch(field_id):
                raise ValueError("INVALID_FIELD_ID")
            identity = field_id.casefold()
            if identity in identities:
                raise ValueError("DUPLICATE_FIELD_ID")
            identities.add(identity)
            if field_type.casefold() != field_type:
                raise ValueError("UNSUPPORTED_FIELD_TYPE")
            field_type = field_type.casefold()
            for flag in ("required", "conditional", "repeatable"):
                if type(raw.get(flag)) is not bool:
                    raise ValueError("INVALID_FIELD_FLAGS")

            conditional_on = raw.get("conditional_on")
            if raw["conditional"]:
                if (not isinstance(conditional_on, str)
                        or not _IDENTIFIER.fullmatch(conditional_on)):
                    raise ValueError("INVALID_CONDITIONAL_METADATA")
                review_reasons.add("CONDITIONAL_FIELD")
            elif conditional_on is not None:
                raise ValueError("UNEXPECTED_CONDITIONAL_METADATA")

            repeatable_group = raw.get("repeatable_group")
            if raw["repeatable"]:
                if (not isinstance(repeatable_group, str)
                        or not _IDENTIFIER.fullmatch(repeatable_group)):
                    raise ValueError("INVALID_REPEATABLE_METADATA")
                review_reasons.add("REPEATABLE_FIELD")
            elif repeatable_group is not None:
                raise ValueError("UNEXPECTED_REPEATABLE_METADATA")

            sensitivity = _sensitivity(field_id, label, field_type)
            supported = field_type in _SUPPORTED_TYPES
            if not supported:
                review_reasons.add("UNSUPPORTED_FIELD_TYPE")
            if sensitivity != "STANDARD":
                review_reasons.add("SENSITIVE_FIELD")
            parsed.append({
                "id": field_id,
                "label": label,
                "type": field_type,
                "required": raw["required"],
                "conditional": raw["conditional"],
                "conditional_on": conditional_on,
                "repeatable": raw["repeatable"],
                "repeatable_group": repeatable_group,
                "sensitivity": sensitivity,
                "supported": supported,
                "requires_human_review": (
                    raw["conditional"] or raw["repeatable"] or sensitivity != "STANDARD"
                    or not supported
                ),
            })
    except ValueError as error:
        return _unknown(str(error))

    id_set = {field["id"].casefold() for field in parsed}
    if any(field["conditional_on"] and field["conditional_on"].casefold() not in id_set
           for field in parsed):
        return _unknown("UNKNOWN_CONDITIONAL_CONTROLLER")

    if review_reasons:
        status = "REVIEW_REQUIRED"
        reason = ",".join(sorted(review_reasons))
    else:
        status = "PASS"
        reason = None
    return {
        "provider": "workday",
        "schema_version": 1,
        "status": status,
        "reason": reason,
        "form_id": form_id,
        "revision": revision,
        "fields": parsed,
    }


def requires_workday_human_review(analysis) -> bool:
    """Return true unless every field has supported, unchanged known semantics."""
    if (not isinstance(analysis, Mapping) or analysis.get("provider") != "workday"
            or analysis.get("status") != "PASS"):
        return True
    fields = analysis.get("fields")
    if not isinstance(fields, list) or not fields:
        return True
    for field in fields:
        if (not isinstance(field, Mapping) or field.get("supported") is not True
                or field.get("requires_human_review") is not False
                or field.get("sensitivity") != "STANDARD"
                or field.get("conditional") is not False
                or field.get("repeatable") is not False):
            return True
    return False


def validate_workday_form(metadata: Mapping, expected_analysis: Mapping) -> dict:
    """Reanalyze metadata and reject any schema, revision, or field change.

    Only the analyzer's safe projection is compared. A malformed prior snapshot
    or any mismatch produces a fixed value-free UNKNOWN result.
    """
    if (not isinstance(expected_analysis, Mapping)
            or expected_analysis.get("provider") != "workday"
            or expected_analysis.get("status") not in {"PASS", "REVIEW_REQUIRED"}
            or not isinstance(expected_analysis.get("fields"), list)
            or not expected_analysis.get("fields")):
        return _unknown("INVALID_EXPECTED_ANALYSIS")
    current = analyze_workday_form(metadata)
    if current.get("status") == "UNKNOWN":
        return current
    if current != expected_analysis:
        return _unknown("FORM_CHANGED")
    return current
