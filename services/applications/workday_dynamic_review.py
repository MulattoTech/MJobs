"""Value-free structural identity for review-only Workday form observations.

Question text is hashed locally, never persisted here. These hashes describe an
observation; they grant no permission to fill, authenticate, upload or submit.
Route and profile freshness remain the durable consumer's responsibility.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re


_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-\[\]]{0,99}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TYPES = {
    "text", "email", "tel", "url", "number", "date", "select", "textarea",
    "checkbox", "radio", "file", "password",
}
_FIELDS = {
    "id", "name", "type", "required", "question_fingerprint", "conditional",
    "conditional_on", "repeatable", "repeatable_group",
}
_ANALYSIS_FIELDS = {
    "id", "name", "label", "type", "required", "conditional", "conditional_on",
    "repeatable", "repeatable_group", "sensitivity", "supported", "requires_human_review",
    "options_fingerprint",
}


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def _identifier(value, *, nullable=False):
    return value is None and nullable or isinstance(value, str) and bool(_ID.fullmatch(value))


def validate_workday_dynamic_review(envelope) -> dict | None:
    """Validate an exact bounded schema and return detached safe metadata.

    Invalid observations never become empty valid forms. Dependencies must refer
    to unique observed controls and form an acyclic graph. Unknown semantics are
    represented by null flags and remain subject to human review downstream.
    """
    if (not isinstance(envelope, Mapping)
            or set(envelope) != {"schema_version", "fingerprint", "fields"}
            or type(envelope.get("schema_version")) is not int
            or envelope["schema_version"] != 1
            or not isinstance(envelope.get("fingerprint"), str)
            or not _HASH.fullmatch(envelope["fingerprint"])
            or not isinstance(envelope.get("fields"), list)
            or not 1 <= len(envelope["fields"]) <= 100):
        return None
    fields = []
    identities = set()
    for source in envelope["fields"]:
        if not isinstance(source, Mapping) or set(source) != _FIELDS:
            return None
        if (not _identifier(source["id"], nullable=True)
                or not _identifier(source["name"], nullable=True)
                or not (source["id"] or source["name"])
                or not isinstance(source["type"], str) or source["type"] not in _TYPES
                or type(source["required"]) is not bool
                or not isinstance(source["question_fingerprint"], str)
                or not _HASH.fullmatch(source["question_fingerprint"])):
            return None
        for flag, reference in (("conditional", "conditional_on"),
                                ("repeatable", "repeatable_group")):
            if source[flag] is not None and type(source[flag]) is not bool:
                return None
            if source[flag] is True:
                if not _identifier(source[reference]):
                    return None
            elif source[reference] is not None:
                return None
        identity = (source["id"] or source["name"]).casefold()
        if identity in identities:
            return None
        identities.add(identity)
        fields.append({key: source[key] for key in _FIELDS})
    dependencies = {
        (field["id"] or field["name"]).casefold(): field["conditional_on"].casefold()
        for field in fields if field["conditional_on"] is not None
    }
    for identity, parent in dependencies.items():
        seen = {identity}
        while parent is not None:
            if parent not in identities or parent in seen:
                return None
            seen.add(parent)
            parent = dependencies.get(parent)
    body = {"schema_version": 1, "fields": fields}
    if _digest(body) != envelope["fingerprint"]:
        return None
    return {**body, "fingerprint": envelope["fingerprint"]}


def project_workday_dynamic_review(analysis) -> dict | None:
    """Hash supported analyzer metadata into a strict durable observation.

    The existing analyzer excludes input/selected/textarea answers. This second
    boundary rejects unexpected field keys, hashes question labels, and retains
    only structural descriptors. Form/revision changes alter question identity.
    """
    if (not isinstance(analysis, Mapping) or analysis.get("provider") != "workday"
            or type(analysis.get("schema_version")) is not int
            or analysis["schema_version"] != 1
            or not isinstance(analysis.get("status"), str)
            or analysis["status"] not in {"PASS", "REVIEW_REQUIRED"}
            or not _identifier(analysis.get("form_id"))
            or not _identifier(analysis.get("revision"))
            or not isinstance(analysis.get("fields"), list)
            or not 1 <= len(analysis["fields"]) <= 100):
        return None
    fields = []
    for source in analysis["fields"]:
        if (not isinstance(source, Mapping) or not set(source).issubset(_ANALYSIS_FIELDS)
                or not (_ANALYSIS_FIELDS - {"name", "options_fingerprint"}).issubset(source)
                or not isinstance(source["sensitivity"], str)
                or source["sensitivity"] not in {"STANDARD", "SENSITIVE", "CREDENTIAL", "UNKNOWN"}
                or type(source["supported"]) is not bool
                or type(source["requires_human_review"]) is not bool):
            return None
        label = source["label"]
        if label is not None and (not isinstance(label, str) or len(label) > 500):
            return None
        options_fingerprint = source.get("options_fingerprint")
        if options_fingerprint is not None and (
                not isinstance(options_fingerprint, str) or not _HASH.fullmatch(options_fingerprint)):
            return None
        field = {key: source.get(key) for key in _FIELDS - {"question_fingerprint"}}
        if (not isinstance(field["type"], str) or field["type"] not in _TYPES
                or type(field["required"]) is not bool
                or any(not _identifier(field[key], nullable=True)
                       for key in ("id", "name", "conditional_on", "repeatable_group"))
                or any(field[key] is not None and type(field[key]) is not bool
                       for key in ("conditional", "repeatable"))):
            return None
        field["question_fingerprint"] = _digest({
            "form_id": analysis["form_id"], "revision": analysis["revision"],
            "field": field, "label": " ".join(label.split()) if label else None,
            "sensitivity": source["sensitivity"], "supported": source["supported"],
            "requires_human_review": source["requires_human_review"],
            "options_fingerprint": options_fingerprint,
        })
        fields.append(field)
    body = {"schema_version": 1, "fields": fields}
    return validate_workday_dynamic_review({**body, "fingerprint": _digest(body)})
