"""Bounded literal-term audit of an already validated, saved resume preview.

This module does not validate files, resolve facts, or look up current targets.
Its source pointers describe recorded metadata; they are not an independent
cryptographic attestation of that metadata or a claim of candidate qualification.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata


MAX_TARGET_TERMS = 100
MAX_TERM_CHARS = 120
MAX_PREVIEW_CHARS = 200_000
MAX_VERSION = 2**53 - 1
METHOD = "CASEFOLD_LITERAL_TERMS_V1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _unavailable(reason: str, *, validated: bool) -> dict:
    return {
        "schema_version": 1,
        "method": METHOD,
        "status": "UNAVAILABLE",
        "reason": reason,
        "factual_validation": "VALIDATED_SAVED_SOURCES" if validated else "NOT_VALIDATED",
        "source": None,
        "present_terms": [],
        "missing_terms": [],
        "term_count": 0,
    }


def _identifier(value: object) -> bool:
    return type(value) is str and len(value) <= 128 and _IDENTIFIER.fullmatch(value) is not None


def _digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and _SHA256.fullmatch(value) is not None


def _version(value: object) -> bool:
    # JSON consumers must be able to bind this revision without rounding it.
    return type(value) is int and 1 <= value <= MAX_VERSION


def _safe_unicode(value: str) -> bool:
    # Whitespace is normalized, while invisible controls and lone surrogates
    # cannot become displayed target labels or split an apparent whole term.
    return all(character.isspace() or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
               for character in value)


def _normal_whitespace(value: str) -> str:
    return " ".join(value.split())


def _identifier_character(character: str) -> bool:
    return (character.isalnum() or character in "_+#"
            or unicodedata.category(character).startswith("M"))


def _joined_identifier(text: str, position: int, direction: int) -> bool:
    if not 0 <= position < len(text):
        return False
    character = text[position]
    if _identifier_character(character):
        return True
    # An internal dot continues identifiers such as ASP.NET or Python.py;
    # sentence punctuation does not. Hyphens and slashes delimit terms in
    # phrases/lists such as Python-based and C/C++.
    adjacent = position + direction
    return (character == "." and 0 <= adjacent < len(text)
            and _identifier_character(text[adjacent]))


def _contains_literal_term(text: str, term: str) -> bool:
    pattern = re.compile(re.escape(term))
    position = 0
    while (match := pattern.search(text, position)) is not None:
        start, end = match.span()
        if (not _joined_identifier(text, start - 1, -1)
                and not _joined_identifier(text, end, 1)):
            return True
        # Rejected matches can overlap a later valid multiword occurrence.
        position = start + 1
    return False


def audit_saved_resume(document: object, preview_text: object, *, validation_state: object) -> dict:
    """Report recorded target terms literally present in validated saved text.

    Whitespace is collapsed and Unicode casefolding is applied for comparison.
    Case/whitespace duplicates keep their first cleaned spelling and order.
    Source ``target_terms_sha256`` hashes compact UTF-8 JSON of the original
    recorded ``job_skills`` list, before deduplication or normalization. The
    recorded TXT checksum is retained; it is not a hash of the preview string.
    No aliases, stemming, qualification scores, or suggested claims are added.
    """
    validated = type(validation_state) is str and validation_state == "VALIDATED"
    if not validated or type(preview_text) is not str:
        return _unavailable("DOCUMENT_NOT_VALIDATED", validated=False)

    if (type(document) is not dict or type(document.get("kind")) is not str
            or document["kind"] != "resume"
            or not _identifier(document.get("id"))
            or not _identifier(document.get("job_id"))
            or not _version(document.get("version"))
            or not _identifier(document.get("profile_version"))
            or type(document.get("sha256")) is not dict
            or not _digest(document["sha256"].get("txt"))):
        return _unavailable("DOCUMENT_IDENTITY_INVALID", validated=True)

    if len(preview_text) > MAX_PREVIEW_CHARS:
        return _unavailable("AUDIT_INPUT_LIMIT_EXCEEDED", validated=True)
    if not _safe_unicode(preview_text):
        return _unavailable("DOCUMENT_NOT_VALIDATED", validated=False)
    text = _normal_whitespace(preview_text).casefold()
    if not text:
        return _unavailable("DOCUMENT_NOT_VALIDATED", validated=False)
    if len(text) > MAX_PREVIEW_CHARS:
        return _unavailable("AUDIT_INPUT_LIMIT_EXCEEDED", validated=True)
    if document.get("responsibility_context_valid") is False:
        return _unavailable("TARGET_SNAPSHOT_INVALID", validated=True)

    context = document.get("generation_context")
    if context is None:
        return _unavailable("TARGET_SNAPSHOT_MISSING", validated=True)
    if type(context) is not dict:
        return _unavailable("TARGET_SNAPSHOT_INVALID", validated=True)
    mode = context.get("mode")
    if mode is None or (type(mode) is str and mode != "APPROVED_SKILL_ORDER"):
        return _unavailable("TARGET_SNAPSHOT_MISSING", validated=True)
    if (type(mode) is not str or not _digest(context.get("target_fingerprint"))
            or not _identifier(context.get("job_id"))
            or context["job_id"] != document["job_id"]
            or type(context.get("job_skills")) is not list):
        return _unavailable("TARGET_SNAPSHOT_INVALID", validated=True)

    recorded_terms = context["job_skills"]
    if len(recorded_terms) > MAX_TARGET_TERMS:
        return _unavailable("AUDIT_INPUT_LIMIT_EXCEEDED", validated=True)
    if not recorded_terms:
        return _unavailable("NO_TARGET_TERMS", validated=True)

    terms = []
    seen = set()
    for value in recorded_terms:
        if type(value) is not str:
            return _unavailable("TARGET_SNAPSHOT_INVALID", validated=True)
        if len(value) > MAX_TERM_CHARS:
            return _unavailable("AUDIT_INPUT_LIMIT_EXCEEDED", validated=True)
        if not _safe_unicode(value):
            return _unavailable("TARGET_SNAPSHOT_INVALID", validated=True)
        label = _normal_whitespace(value)
        normalized = label.casefold()
        if not normalized:
            return _unavailable("TARGET_SNAPSHOT_INVALID", validated=True)
        if len(normalized) > MAX_TERM_CHARS:
            return _unavailable("AUDIT_INPUT_LIMIT_EXCEEDED", validated=True)
        if normalized not in seen:
            terms.append((label, normalized))
            seen.add(normalized)

    present = []
    missing = []
    for label, normalized in terms:
        (present if _contains_literal_term(text, normalized) else missing).append(label)

    return {
        "schema_version": 1,
        "method": METHOD,
        "status": "AVAILABLE",
        "reason": "RECORDED_TARGET_TERMS",
        "factual_validation": "VALIDATED_SAVED_SOURCES",
        "source": {
            "document_id": document["id"],
            "document_version": document["version"],
            "profile_version": document["profile_version"],
            "text_sha256": document["sha256"]["txt"],
            "target_fingerprint": context["target_fingerprint"],
            "target_terms_sha256": hashlib.sha256(json.dumps(
                recorded_terms, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "job_id": document["job_id"],
        },
        "present_terms": present,
        "missing_terms": missing,
        "term_count": len(terms),
    }
