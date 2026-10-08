"""Read-only questionnaire capability checks before any application browser starts."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_UNEXPECTED_SENSITIVE_QUESTION = re.compile(
    r"\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|"
    r"date of birth|birth date|social security(?: number)?|ssn|national id|national origin|"
    r"age|marital|citizenship|citizen|authoriz\w*|work authorization|"
    r"eligible to work|work eligibility|right to work|permi(?:tted|ssion) to work|allow\w* to work|"
    r"visa|sponsorship|criminal|convict\w*|felon\w*|background (?:check|screen\w*)|"
    r"legally authorized|salary expectation\w*|salary range|"
    r"desired compensation|compensation expectation\w*|non[ -]?compete|conflict\w* of interest|"
    r"minimum age|attest\w*|certif\w* under penalty)\b", re.IGNORECASE)
_PROFILE_PATHS = {"name", "email"}
_FACT_KEYS = {"fact_ids", "answer_ids", "profile_paths", "document_kinds", "application_context"}
_DOCUMENT_KINDS = {"resume", "cover-letter"}
_APPLICATION_CONTEXT = {"application_id"}
MAX_QUESTIONS = 200
MAX_QUESTION_LABEL = 300


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
        if any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
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
        if question["required"] and _is_unexpected_sensitive_question(question):
            sensitive = _unknown(profile, route, questionnaire, document_kinds,
                                 "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION")
            sensitive["missing"] = [{
                "question_id": question["id"], "question": question["label"],
            }]
            return sensitive
        mapping = _valid_mapping(question)
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
        absent = {
            "fact_ids": [value for value in mapping["fact_ids"] if value not in fact_ids],
            "answer_ids": [value for value in mapping["answer_ids"] if value not in answer_ids],
            "profile_paths": [value for value in mapping["profile_paths"]
                              if not _profile_value(profile, value)],
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


def _is_unexpected_sensitive_question(question):
    """Recognize high-risk/legal question wording without evaluating any answers."""
    return _UNEXPECTED_SENSITIVE_QUESTION.search(question["label"]) is not None


def persist_preflight(store, application_id: str, result: dict) -> None:
    if result.get("result") not in ("PASS", "FAIL", "UNKNOWN"):
        raise ValueError("Unsupported question preflight result")
    with store.engine.begin() as connection:
        store._audit(connection, application_id, "APPLICATION_QUESTION_PREFLIGHT", {
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
            "mappings": result.get("mappings", []),
            "missing": result.get("missing", []),
        })


def latest_preflight(store, application_id: str) -> dict | None:
    with store.engine.connect() as connection:
        row = connection.execute(select(m.audit).where(
            m.audit.c.entity_id == application_id,
            m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).mappings().one_or_none()
    if row is None:
        return None
    return {**row["payload"], "event_id": row["id"]}
