"""Read-only questionnaire capability checks before any application browser starts."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PROFILE_PATH = re.compile(r"^[A-Za-z0-9_-]{1,80}(?:\.[A-Za-z0-9_-]{1,80})?$")
_UNEXPECTED_SENSITIVE_QUESTION = re.compile(
    r"\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|"
    r"date of birth|birth date|social security(?: number)?|ssn|national id|national origin|"
    r"age|marital|citizenship|citizen|authoriz\w*|work authorization|"
    r"eligible to work|work eligibility|right to work|permi(?:tted|ssion) to work|allow\w* to work|"
    r"visa|sponsorship|criminal|convict\w*|felon\w*|background (?:check|screen\w*)|"
    r"legally authorized|salary expectation\w*|salary range|"
    r"desired compensation|compensation expectation\w*|non[ -]?compete|conflict\w* of interest|"
    r"minimum age|attest\w*|certif\w* under penalty)\b", re.IGNORECASE)
_PROFILE_PATHS = {
    "name", "email", "preferences.work_authorization", "preferences.sponsorship_needs",
}
_LEGAL_PROFILE_PATHS = {
    "preferences.work_authorization", "preferences.sponsorship_needs",
}
_SUPPORTED_WORK_AUTHORIZATION_QUESTION = re.compile(
    r"^\s*(?:(?:are you\s+)?(?:legally\s+)?authorized to work|"
    r"(?:are you\s+)?legally authorized for employment|"
    r"(?:are you\s+)?(?:eligible|permitted) to work|"
    r"(?:do you have\s+)?legal permission to work|"
    r"(?:what is your\s+)?work authorization)(?:\s+in\s+(?:the\s+)?"
    r"(?:u\.?s\.?a?\.?|united states|canada|country of employment))?\s*[?.!]?$",
    re.IGNORECASE)
_SUPPORTED_SPONSORSHIP_QUESTION = re.compile(
    r"^\s*(?:do|will|would) you (?:(?:now|currently)(?: or in the future)? )?"
    r"(?:need|require) (?:employer )?(?:visa )?sponsorship"
    r"(?: for (?:employment )?visa(?: status)?)?\s*[?.!]?$", re.IGNORECASE)
_AMBIGUOUS_WORK_AUTHORIZATION = re.compile(
    r"\b(?:unknown|unspecified|not specified|not sure|unsure|uncertain|maybe|"
    r"i think|i believe|i expect|i hope|i guess|i assume|think|believe|expect|hope|guess|assume|"
    r"probably|likely|possibly|potentially|might|may|could|should|"
    r"prefer not to answer|decline to answer|do not know|don't know)\b", re.IGNORECASE)
_FACT_KEYS = {"fact_ids", "answer_ids", "profile_paths", "document_kinds", "application_context"}
_DOCUMENT_KINDS = {"resume", "cover-letter"}
_APPLICATION_CONTEXT = {"application_id"}
MAX_QUESTIONS = 200
MAX_QUESTION_LABEL = 300
_DYNAMIC_TYPES = {"text", "email", "number", "date", "file", "select", "boolean", "choice"}
_DYNAMIC_QUESTION_KEYS = {
    "fact_ids", "answer_ids", "profile_paths", "document_kinds", "application_context",
}
_DYNAMIC_META_KEYS = {
    "metadata_provider", "metadata_tenant", "metadata_requisition", "metadata_revision",
}


def questionnaire_fingerprint(questionnaire: dict) -> str:
    canonical = json.dumps(questionnaire, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _base_result(profile, route, questionnaire, document_kinds):
    return {
        "result": "UNKNOWN",
        "reason": None,
        "profile_version": profile.get("version"),
        "route_id": route.get("id"),
        "provider": route.get("provider"),
        "tenant": route.get("tenant"),
        "requisition": route.get("requisition"),
        "observed_at": now(),
        "metadata_fingerprint": None,
        "document_kinds": sorted(document_kinds),
        "mappings": [],
        "missing": [],
    }


def _unknown(profile, route, questionnaire, document_kinds, reason):
    result = _base_result(profile, route, questionnaire, document_kinds)
    result["reason"] = reason
    if isinstance(questionnaire, dict):
        try:
            result["metadata_fingerprint"] = questionnaire_fingerprint(questionnaire)
        except (TypeError, ValueError):
            pass
    return result


def _valid_mapping(question):
    mapping = question.get("requirements")
    if not isinstance(mapping, dict) or any(key not in _FACT_KEYS for key in mapping):
        return None
    clean = {}
    allowed = {
        "fact_ids": None,
        "answer_ids": None,
        "profile_paths": _PROFILE_PATHS,
        "document_kinds": _DOCUMENT_KINDS,
        "application_context": _APPLICATION_CONTEXT,
    }
    for key, choices in allowed.items():
        values = mapping.get(key, [])
        if not isinstance(values, list) or len(values) > 50:
            return None
        pattern = _PROFILE_PATH if key == "profile_paths" else _IDENTIFIER
        if any(not isinstance(value, str) or not pattern.fullmatch(value)
               for value in values):
            return None
        if choices is not None and any(value not in choices for value in values):
            return None
        if len(set(values)) != len(values):
            return None
        clean[key] = list(values)
    return clean


def _approved(rows):
    return {
        row.get("fact_id", row.get("id"))
        for row in rows
        if isinstance(row, dict)
        and row.get("verified") is True
        and row.get("user_approved") is True
        and type(row.get("confidence")) in (int, float)
        and row["confidence"] >= 80
        and isinstance(row.get("source"), str) and row["source"].strip()
        and isinstance(row.get("text"), str) and row["text"].strip()
    }


def build_preflight_result(profile: dict, route: dict, questionnaire: dict | None,
                           document_kinds: set[str], application_id: str | None = None) -> dict:
    """Evaluate supported structured questions against one immutable profile snapshot."""
    if route.get("provider") != "mock-ats" or route.get("tenant") != "example":
        return _unknown(profile, route, questionnaire, document_kinds, "UNSUPPORTED_ATS")
    if not isinstance(questionnaire, dict):
        return _unknown(profile, route, questionnaire, document_kinds, "QUESTION_METADATA_UNAVAILABLE")
    if (questionnaire.get("provider") != route.get("provider")
            or questionnaire.get("tenant") != route.get("tenant")
            or questionnaire.get("requisition") != route.get("requisition")
            or type(questionnaire.get("revision")) is not int
            or questionnaire["revision"] < 1
            or not isinstance(questionnaire.get("questions"), list)
            or len(questionnaire["questions"]) > MAX_QUESTIONS):
        return _unknown(profile, route, questionnaire, document_kinds, "INVALID_QUESTION_METADATA")

    result = _base_result(profile, route, questionnaire, document_kinds)
    try:
        result["metadata_fingerprint"] = questionnaire_fingerprint(questionnaire)
    except (TypeError, ValueError):
        return _unknown(profile, route, questionnaire, document_kinds, "INVALID_QUESTION_METADATA")

    fact_ids = _approved(profile.get("facts", []))
    answer_ids = _approved(profile.get("answers", []))
    missing = []
    seen = set()
    for question in questionnaire["questions"]:
        if (not isinstance(question, dict)
                or not isinstance(question.get("id"), str)
                or not _IDENTIFIER.fullmatch(question["id"])
                or not isinstance(question.get("label"), str)
                or not question["label"].strip()
                or len(question["label"]) > MAX_QUESTION_LABEL
                or type(question.get("required")) is not bool
                or question["id"] in seen):
            return _unknown(profile, route, questionnaire, document_kinds, "INVALID_QUESTION_METADATA")
        seen.add(question["id"])
        mapping = _valid_mapping(question)
        legal_path = _legal_profile_mapping(question, mapping)
        if (question["required"] and _is_unexpected_sensitive_question(question)
                and legal_path is None):
            sensitive = _unknown(profile, route, questionnaire, document_kinds,
                                 "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")
            sensitive["missing"] = [{
                "question_id": question["id"], "question": question["label"],
            }]
            return sensitive
        if (question["required"] and mapping is not None
                and any(path in _LEGAL_PROFILE_PATHS for path in mapping["profile_paths"])
                and legal_path is None):
            return _unknown(profile, route, questionnaire, document_kinds,
                            "UNMAPPED_REQUIRED_QUESTION")
        if mapping is None:
            if question["required"]:
                return _unknown(profile, route, questionnaire, document_kinds,
                                "UNMAPPED_REQUIRED_QUESTION")
            continue
        mapped_count = sum(map(len, mapping.values()))
        if question["required"] and not mapped_count:
            return _unknown(profile, route, questionnaire, document_kinds,
                            "UNMAPPED_REQUIRED_QUESTION")
        row = {"question_id": question["id"], "question": question["label"],
               **mapping}
        result["mappings"].append(row)
        if not question["required"]:
            continue
        if legal_path is not None and not _legal_profile_value(profile, legal_path):
            unavailable = _unknown(profile, route, questionnaire, document_kinds,
                                   "LEGAL_PROFILE_VALUE_UNAVAILABLE")
            unavailable["missing"] = [{
                "question_id": question["id"], "question": question["label"],
            }]
            return unavailable
        absent = {
            "fact_ids": [value for value in mapping["fact_ids"] if value not in fact_ids],
            "answer_ids": [value for value in mapping["answer_ids"] if value not in answer_ids],
            "profile_paths": [value for value in mapping["profile_paths"]
                              if (value != legal_path and not _profile_value(profile, value))],
            "document_kinds": [value for value in mapping["document_kinds"]
                               if value not in document_kinds],
            "application_context": [value for value in mapping["application_context"]
                                    if value == "application_id" and not application_id],
        }
        if any(absent.values()):
            missing.append({"question_id": question["id"], "question": question["label"],
                            **absent})
    result["missing"] = missing
    result["result"] = "FAIL" if missing else "PASS"
    return result


def _profile_value(profile, path):
    value = profile.get(path)
    return isinstance(value, str) and bool(value.strip())


def _legal_profile_mapping(question, mapping):
    """Return the one supported legal profile field only for its exact question class."""
    if mapping is None or any(mapping[key] for key in (
            "fact_ids", "answer_ids", "document_kinds", "application_context")):
        return None
    paths = mapping["profile_paths"]
    if len(paths) != 1:
        return None
    path = paths[0]
    label = question["label"]
    has_work_authorization = _SUPPORTED_WORK_AUTHORIZATION_QUESTION.fullmatch(label) is not None
    has_sponsorship = _SUPPORTED_SPONSORSHIP_QUESTION.fullmatch(label) is not None
    # A compound question requires two answers. This preflight intentionally
    # supports only one legal answer class per structured question.
    if has_work_authorization == has_sponsorship:
        return None
    if path == "preferences.work_authorization" and has_work_authorization:
        return path
    if path == "preferences.sponsorship_needs" and has_sponsorship:
        return path
    return None


def _legal_profile_value(profile, path):
    preferences = profile.get("preferences")
    if not isinstance(preferences, dict):
        return False
    value = preferences.get(path.split(".", 1)[1])
    if path == "preferences.sponsorship_needs":
        return value in {"REQUIRED", "NOT_REQUIRED"}
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.split()).casefold()
    return (bool(normalized) and normalized not in {"n/a", "na"}
            and _AMBIGUOUS_WORK_AUTHORIZATION.search(normalized) is None)


def _is_unexpected_sensitive_question(question):
    """Recognize high-risk/legal question wording without evaluating any answers."""
    return _UNEXPECTED_SENSITIVE_QUESTION.search(question["label"]) is not None


def _bounded_string_list(value, *, maximum, pattern=None, label=False):
    if not isinstance(value, list) or len(value) > maximum:
        return None
    output = []
    for item in value:
        if not isinstance(item, str):
            return None
        clean = " ".join(item.split()) if label else item
        if not clean or (label and len(clean) > MAX_QUESTION_LABEL):
            return None
        if pattern is not None and not pattern.fullmatch(clean):
            return None
        if clean in output:
            return None
        output.append(clean)
    return output


def build_dynamic_form_snapshot(result: dict) -> dict | None:
    """Return bounded, value-free form metadata suitable for durable readback.

    The upstream fingerprint remains the browser freshness token. A second hash
    binds the exact safe projection retained by this service.
    """
    fingerprint = result.get("metadata_fingerprint")
    rows = result.get("mappings")
    if (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or not isinstance(rows, list) or not rows or len(rows) > MAX_QUESTIONS):
        return None

    scope = None
    fields = []
    sections: dict[str, list[str]] = {}
    groups: dict[str, dict] = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        field_id = row.get("field_id")
        question_id = row.get("question_id")
        label = row.get("question")
        field_type = row.get("type")
        if (not isinstance(field_id, str) or not _IDENTIFIER.fullmatch(field_id)
                or field_id in seen
                or not isinstance(question_id, str) or not _IDENTIFIER.fullmatch(question_id)
                or not isinstance(label, str) or not label.strip()
                or len(label) > MAX_QUESTION_LABEL
                or not isinstance(field_type, str) or field_type not in _DYNAMIC_TYPES
                or type(row.get("type_explicit")) is not bool
                or type(row.get("required")) is not bool
                or type(row.get("visible")) is not bool
                or type(row.get("optional")) is not bool
                or type(row.get("conditional")) is not bool
                or type(row.get("repeatable")) is not bool
                or type(row.get("supported", False)) is not bool
                or type(row.get("manual_review_required", True)) is not bool
                or type(row.get("ambiguous", True)) is not bool
                or row.get("sensitivity", "UNKNOWN") not in {"STANDARD", "SENSITIVE", "CREDENTIAL", "UNKNOWN"}):
            return None
        seen.add(field_id)

        provider, tenant, requisition, revision = (
            row.get("metadata_provider"), row.get("metadata_tenant"),
            row.get("metadata_requisition"), row.get("metadata_revision"),
        )
        row_scope = (provider, tenant, requisition, revision)
        if (not isinstance(provider, str) or not _IDENTIFIER.fullmatch(provider)
                or not isinstance(tenant, str) or not _IDENTIFIER.fullmatch(tenant)
                or not isinstance(requisition, str) or not _IDENTIFIER.fullmatch(requisition)
                or type(revision) is not int or revision < 1
                or (scope is not None and row_scope != scope)):
            return None
        scope = row_scope

        options = _bounded_string_list(row.get("options"), maximum=100, label=True)
        section_ids = _bounded_string_list(row.get("optional_sections"), maximum=50,
                                           pattern=_IDENTIFIER)
        if options is None or section_ids is None:
            return None
        depends_on = row.get("depends_on")
        if depends_on is not None and (
                not isinstance(depends_on, str) or not _IDENTIFIER.fullmatch(depends_on)):
            return None

        field = {
            "field_id": field_id,
            "question_id": question_id,
            "label": " ".join(label.split()),
            "type": field_type,
            "type_explicit": row["type_explicit"],
            "required": row["required"],
            "visible": row["visible"],
            "optional": row["optional"],
            "conditional": row["conditional"],
            "depends_on": depends_on,
            "options": options,
            "repeatable": row["repeatable"],
            "optional_sections": section_ids,
            "supported": row.get("supported") is True,
            "manual_review_required": row.get("manual_review_required", True) is True,
            "ambiguous": row.get("ambiguous", True) is True,
            "sensitivity": row.get("sensitivity", "UNKNOWN"),
        }
        if field["type_explicit"] is not True:
            field["supported"] = False
            field["manual_review_required"] = True
        if field["sensitivity"] != "STANDARD" or field["ambiguous"]:
            field["supported"] = False
            field["manual_review_required"] = True
            field["options"] = []
            field["label"] = "Sensitive or ambiguous question requires manual review"

        condition = row.get("condition")
        if condition is not None:
            if (not isinstance(condition, dict) or set(condition) != {"question_id", "equals"}
                    or not isinstance(condition.get("question_id"), str)
                    or not _IDENTIFIER.fullmatch(condition["question_id"])
                    or not isinstance(condition.get("equals"), str)
                    or len(condition["equals"]) > MAX_QUESTION_LABEL):
                return None
            field["condition"] = {"question_id": condition["question_id"],
                                  "equals": " ".join(condition["equals"].split())}
        options_by = row.get("options_by")
        if options_by is not None:
            if (not isinstance(options_by, dict) or set(options_by) != {"question_id", "choices"}
                    or not isinstance(options_by.get("question_id"), str)
                    or not _IDENTIFIER.fullmatch(options_by["question_id"])
                    or not isinstance(options_by.get("choices"), dict)
                    or not 1 <= len(options_by["choices"]) <= 100):
                return None
            choices = {}
            for parent_value, child_options in options_by["choices"].items():
                if not isinstance(parent_value, str) or len(parent_value) > MAX_QUESTION_LABEL:
                    return None
                normalized = _bounded_string_list(child_options, maximum=100, label=True)
                if not normalized:
                    return None
                choices[" ".join(parent_value.split())] = normalized
            field["options_by"] = {"question_id": options_by["question_id"],
                                    "choices": choices}
        for section_id in section_ids:
            sections.setdefault(section_id, []).append(field_id)

        if field["repeatable"]:
            group_id = row.get("repeatable_group")
            minimum = row.get("repeatable_min_rows")
            maximum = row.get("repeatable_max_rows")
            group_required = row.get("repeatable_group_required")
            group_visible = row.get("repeatable_group_visible")
            if (not isinstance(group_id, str) or not _IDENTIFIER.fullmatch(group_id)
                    or type(minimum) is not int or type(maximum) is not int
                    or minimum < 0 or maximum < minimum or maximum > 20
                    or type(group_required) is not bool or type(group_visible) is not bool):
                return None
            group = groups.get(group_id)
            details = {"minimum_rows": minimum, "maximum_rows": maximum,
                       "required": group_required, "visible": group_visible}
            if group is not None and any(group[key] != value for key, value in details.items()):
                return None
            if group is None:
                groups[group_id] = {"group_id": group_id, "field_ids": [], **details}
            groups[group_id]["field_ids"].append(field_id)
            field.update({
                "repeatable_group": group_id,
                "repeatable_min_rows": minimum,
                "repeatable_max_rows": maximum,
            })
        elif any(key in row for key in (
                "repeatable_group", "repeatable_min_rows", "repeatable_max_rows",
                "repeatable_group_required", "repeatable_group_visible")):
            return None
        fields.append(field)

    parents_by_id = {field["field_id"]: field["depends_on"] for field in fields}
    for field in fields:
        parent = field["depends_on"]
        if parent is not None:
            parents = [candidate for candidate in fields if candidate["field_id"] == parent]
            if (parent == field["field_id"] or not parents
                    or parents[0]["type"] not in {"select", "choice"}
                    or field["type"] not in {"select", "choice"}):
                return None
    for field_id in parents_by_id:
        seen_dependencies = set()
        current = field_id
        while current is not None:
            if current in seen_dependencies:
                return None
            seen_dependencies.add(current)
            current = parents_by_id.get(current)

    dependent_choices = [
        {"field_id": field["field_id"], "depends_on": field["depends_on"],
         "options": field["options"]}
        for field in fields if field["depends_on"] is not None
    ]
    snapshot = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "scope": {"provider": scope[0], "tenant": scope[1],
                  "requisition": scope[2], "revision": scope[3]},
        "fields": fields,
        "optional_sections": [
            {"section_id": section_id, "field_ids": sorted(field_ids)}
            for section_id, field_ids in sorted(sections.items())
        ],
        "repeatable_groups": [groups[key] for key in sorted(groups)],
        "dependent_choices": dependent_choices,
    }
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False)
    snapshot["snapshot_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return snapshot


def _safe_preflight_rows(rows):
    if not isinstance(rows, list) or len(rows) > MAX_QUESTIONS:
        return []
    safe = []
    for row in rows:
        if (not isinstance(row, dict)
                or not isinstance(row.get("question_id"), str)
                or not _IDENTIFIER.fullmatch(row["question_id"])
                or not isinstance(row.get("question"), str)
                or not row["question"].strip()
                or len(row["question"]) > MAX_QUESTION_LABEL):
            continue
        item = {key: row[key] for key in ("question_id", "question")}
        valid = True
        for key in _DYNAMIC_QUESTION_KEYS:
            values = row.get(key, [])
            choices = (_PROFILE_PATHS if key == "profile_paths" else
                       _DOCUMENT_KINDS if key == "document_kinds" else
                       _APPLICATION_CONTEXT if key == "application_context" else None)
            pattern = _PROFILE_PATH if key == "profile_paths" else _IDENTIFIER
            if (not isinstance(values, list) or len(values) > 50
                    or any(not isinstance(value, str) or not pattern.fullmatch(value)
                           or (choices is not None and value not in choices) for value in values)
                    or len(set(values)) != len(values)):
                valid = False
                break
            item[key] = list(values)
        if valid:
            for key in _DYNAMIC_META_KEYS:
                if key not in row:
                    continue
                value = row[key]
                if key == "metadata_revision":
                    if type(value) is not int or value < 1:
                        valid = False
                        break
                elif not isinstance(value, str) or not value or len(value) > 80:
                    valid = False
                    break
                item[key] = value
        if valid:
            safe.append(item)
    return safe


def persist_preflight(store, application_id: str, result: dict) -> None:
    if result.get("result") not in ("PASS", "FAIL", "UNKNOWN"):
        raise ValueError("Unsupported question preflight result")
    mappings = _safe_preflight_rows(result.get("mappings", []))
    missing = _safe_preflight_rows(result.get("missing", []))
    payload = {
            "result": result["result"],
            "reason": result.get("reason"),
            "observed_at": result["observed_at"],
            "profile_version": result.get("profile_version"),
            "document_version": result.get("document_version"),
            "route_id": result.get("route_id"),
            "provider": result.get("provider"),
            "tenant": result.get("tenant"),
            "requisition": result.get("requisition"),
            "metadata_fingerprint": result.get("metadata_fingerprint"),
            "mappings": mappings,
            "missing": missing,
        }
    snapshot = build_dynamic_form_snapshot(result)
    if snapshot is not None:
        payload.update({
            "dynamic_fields": snapshot["fields"],
            "dynamic_fields_fingerprint": snapshot["fingerprint"],
            "dynamic_form_metadata": snapshot,
            "optional_sections": [
                {"id": section["section_id"], "field_ids": section["field_ids"]}
                for section in snapshot["optional_sections"]
            ],
        })
    with store.engine.begin() as connection:
        store._audit(connection, application_id, "APPLICATION_QUESTION_PREFLIGHT", payload)


def latest_preflight(store, application_id: str) -> dict | None:
    with store.engine.connect() as connection:
        row = connection.execute(select(m.audit).where(
            m.audit.c.entity_id == application_id,
            m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).mappings().one_or_none()
    if row is None:
        return None
    return {**row["payload"], "event_id": row["id"]}
