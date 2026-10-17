"""Value-free reuse decisions shared by answer suggestions and preflight.

A match is advisory evidence only. It does not approve an answer for export,
autofill, or submission. Existing application-specific review remains required.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re

from services.documents.answers import (
    DEFAULT_ANSWER_CONFIDENCE_POLICY,
    _SENSITIVE_TERMS,
    match_answer_question,
    validate_answer_confidence_policy,
)
from services.documents.sensitive_answer_policy import classify_sensitive_category


_MEDICAL = re.compile(
    r"\b(?:medical\w*|health(?:care)?\s+(?:condition|status|history)|"
    r"diagnos\w*|illness\w*|disease\w*|medication\w*|treatment\w*)\b", re.IGNORECASE,
)
_LEGAL_OR_PRIVATE = re.compile(
    r"\b(?:social security(?: number)?|ssn|national id|arrest\w*|"
    r"expected salary|expected compensation|salary requirements?|"
    r"restrictive covenants?|legal certifications?|"
    r"(?:at least |over |under )?\d{1,3}\s*(?:years?\s*(?:of age|old|or older)|\+\s*years?)|"
    r"permi(?:tted|ssion) to work|allow\w* to work)\b", re.IGNORECASE,
)
_BINDING_FIELDS = ("id", "question", "text", "source", "confidence", "verified",
                   "user_approved", "requires_review", "allowed_for_auto_use")


def is_review_sensitive_question(question):
    """Recognize known private, demographic and legal wording without inference."""
    return isinstance(question, str) and bool(
        _SENSITIVE_TERMS.search(question) or _MEDICAL.search(question)
        or _LEGAL_OR_PRIVATE.search(question)
        or classify_sensitive_category(question) is not None
    )


def _snapshot_value(value):
    # Only profile scalar fields participate. Unexpected values cannot introduce
    # executable serialization or leak arbitrary profile objects into a result.
    if value is None or type(value) in (str, bool, int):
        return value
    return {"invalid_type": type(value).__name__}


def assess_answer_review(question, profile_answers, policy=None, *, expected_fingerprint=None):
    """Assess one question against a bounded immutable saved-answer snapshot.

    ``matches`` contains IDs and matching evidence, never answer values. A
    conflicting, unknown, sensitive, or changed snapshot returns no reusable IDs.
    Consumers can bind a pending review to ``fingerprint`` and re-evaluate on
    resume; an old fingerprint cannot establish approval of new answer bytes.
    """
    policy = validate_answer_confidence_policy(
        dict(DEFAULT_ANSWER_CONFIDENCE_POLICY) if policy is None else policy,
    )
    valid_question = isinstance(question, str) and bool(question.strip()) and len(question) <= 2000
    valid_rows = isinstance(profile_answers, (list, tuple)) and len(profile_answers) <= 1000
    rows = [row for row in profile_answers if isinstance(row, dict)] if valid_rows else []
    bindings = [{field: _snapshot_value(row.get(field)) for field in _BINDING_FIELDS} for row in rows]
    # Canonical row order makes database readback ordering immaterial.
    encoded_rows = sorted(json.dumps(row, sort_keys=True, ensure_ascii=True) for row in bindings)
    encoded = json.dumps({"schema": 1, "question": question if valid_question else None,
                          "answers": encoded_rows, "policy": policy,
                          "valid_rows": valid_rows}, sort_keys=True, ensure_ascii=True)
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def decision(reason, matches=None):
        return {"review_required": reason is not None, "reason": reason,
                "fingerprint": fingerprint, "matches": matches or []}

    if expected_fingerprint is not None and (
            not isinstance(expected_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint)
            or not hmac.compare_digest(expected_fingerprint, fingerprint)):
        return decision("CHANGED_ANSWER")
    if not valid_question or not valid_rows:
        return decision("UNKNOWN_ANSWER")
    if is_review_sensitive_question(question):
        return decision("SENSITIVE_QUESTION")

    matches = []
    values = set()
    ids = set()
    for row in rows:
        if (any(not isinstance(row.get(key), str) or not row[key].strip()
                for key in ("id", "question", "text", "source"))
                or row.get("verified") is not True or row.get("user_approved") is not True
                or type(row.get("confidence")) is not int
                or not policy["minimum_saved_answer_confidence"] <= row["confidence"] <= 100):
            continue
        if is_review_sensitive_question(row["question"]):
            continue
        match = match_answer_question(question, row["question"], policy["minimum_question_match_confidence"])
        if match is None:
            continue
        if row.get("requires_review") is True or row.get("allowed_for_auto_use") is False:
            return decision("CHANGED_ANSWER")
        if row["id"] in ids:
            return decision("CONFLICTING_ANSWERS")
        ids.add(row["id"])
        values.add(" ".join(row["text"].split()))
        matches.append({"answer_id": row["id"], **match})
    if len(values) > 1:
        return decision("CONFLICTING_ANSWERS")
    if not matches:
        return decision("UNKNOWN_ANSWER")
    matches.sort(key=lambda item: (-item["score"], item["answer_id"]))
    return decision(None, matches)
