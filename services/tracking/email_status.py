from __future__ import annotations

"""Bounded, deterministic classification of user-provided email text.

This module intentionally returns only a content digest and fixed classification
codes. It never stores or logs the supplied message and never follows links.
"""

from email import policy
from email.message import Message
from email.parser import Parser
import hashlib
import re
from typing import Any

_MAX_CONTENT_BYTES = 256 * 1024
_MAX_MIME_PARTS = 40
_MAX_MIME_DEPTH = 8

# Category order is part of the deterministic conflict/reason-code behavior.
_SIGNAL_PATTERNS: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "CONFIRMATION",
        "APPLIED",
        (
            re.compile(r"\b(?:we\s+)?(?:have\s+)?received\s+your\s+(?:job\s+)?application\b", re.I),
            re.compile(r"\byour\s+application\s+(?:(?:has\s+been\s+)?(?:received|submitted)|was\s+received)\b", re.I),
            re.compile(r"\bapplication\s+(?:receipt|confirmation)\b", re.I),
            re.compile(r"\bthank\s+you\s+for\s+applying\b", re.I),
        ),
    ),
    (
        "REJECTION",
        "REJECTED",
        (
            re.compile(r"\bregret\s+to\s+(?:inform|advise)\b", re.I),
            re.compile(r"\bnot\s+(?:be\s+)?moving\s+forward\b", re.I),
            re.compile(r"\bdecided\s+not\s+to\s+move\s+forward\b", re.I),
            re.compile(r"\bnot\s+selected\b", re.I),
            re.compile(r"\b(?:will\s+)?not\s+be\s+proceeding\b", re.I),
            re.compile(r"\b(?:application|candidacy)\s+(?:was\s+)?rejected\b", re.I),
            re.compile(r"\bdecided\s+to\s+move\s+forward\s+with\s+other\s+candidates\b", re.I),
        ),
    ),
    (
        "INTERVIEW_INVITATION",
        "INTERVIEW",
        (
            re.compile(r"\b(?:invite|invitation)\s+(?:you\s+)?(?:to\s+)?(?:an?\s+)?interview\b", re.I),
            re.compile(r"\b(?:schedule|scheduled|scheduling)\s+(?:an?\s+)?interview\b", re.I),
            re.compile(r"\binterview\s+(?:invitation|request)\b", re.I),
            re.compile(r"\bwould\s+like\s+to\s+interview\s+you\b", re.I),
        ),
    ),
    (
        "RECRUITER_OUTREACH",
        "RECRUITER_CONTACT",
        (
            re.compile(r"\b(?:i am|i'm|this is)\s+(?:a\s+)?recruiter\s+(?:reaching\s+out|contacting)\b", re.I),
            re.compile(r"\brecruiter\s+(?:reaching\s+out|contacting\s+you)\b", re.I),
            re.compile(r"\breaching\s+out\s+about\s+(?:a|an|the)\s+(?:job|role|position|opportunity)\b", re.I),
            re.compile(r"\b(?:role|position|opportunity)\s+(?:may|might)\s+fit\s+your\s+(?:experience|background)\b", re.I),
        ),
    ),
    (
        "OFFER",
        "OFFER",
        (
            re.compile(r"\b(?:pleased|delighted|happy)\s+to\s+offer\s+you\b", re.I),
            re.compile(r"\bformal\s+offer\s+of\s+employment\b", re.I),
            re.compile(r"\boffer\s+you\s+the\s+(?:job|position|role)\b", re.I),
            re.compile(r"\b(?:employment|job)\s+offer\s+(?:is\s+)?(?:attached|enclosed|ready)\b", re.I),
        ),
    ),
)

_REASON_BY_CATEGORY = {
    "CONFIRMATION": "EXPLICIT_CONFIRMATION",
    "REJECTION": "EXPLICIT_REJECTION",
    "INTERVIEW_INVITATION": "EXPLICIT_INTERVIEW_INVITATION",
    "RECRUITER_OUTREACH": "EXPLICIT_RECRUITER_OUTREACH",
    "OFFER": "EXPLICIT_OFFER",
}

# These patterns represent explicit negation of a signal, not rejection language
# such as "not moving forward", which is itself an explicit rejection.
_NEGATED_PATTERNS = (
    re.compile(r"\bnot\s+(?:an?\s+)?offer\b", re.I),
    re.compile(r"\bno\s+(?:job\s+)?offer\b", re.I),
    re.compile(r"\bnot\s+(?:received|submitted)\s+(?:your\s+)?application\b", re.I),
    re.compile(r"\b(?:did\s+not|didn't|never)\s+receive\s+(?:your\s+)?application\b", re.I),
    re.compile(r"\bnot\s+(?:invite|inviting)\s+(?:you\s+)?(?:to\s+)?(?:an?\s+)?interview\b", re.I),
    re.compile(r"\bno\s+interview\s+(?:is\s+)?(?:scheduled|planned)\b", re.I),
    re.compile(r"\bnot\s+(?:a\s+)?rejection\b", re.I),
)
_CONDITIONAL_PATTERNS = (
    re.compile(r"\bif\s+(?:you\s+are\s+)?selected\b", re.I),
    re.compile(r"\bsubject\s+to\s+(?:approval|review|a\s+background\s+check)\b", re.I),
    re.compile(r"\bpending\s+(?:approval|review|a\s+background\s+check)\b", re.I),
    re.compile(r"\b(?:may|might|could)\s+(?:invite|offer|select|reject|move\s+forward)\b", re.I),
    re.compile(r"\bif\b[^.!?]{0,100}\b(?:invite|offer|select|reject|interview)\b", re.I),
)


def _walk_plain_text(message: Message) -> list[str]:
    """Collect only inline text/plain MIME parts under strict traversal bounds."""
    selected: list[str] = []
    parts_seen = 0

    def walk(part: Message, depth: int) -> None:
        nonlocal parts_seen
        parts_seen += 1
        if parts_seen > _MAX_MIME_PARTS or depth > _MAX_MIME_DEPTH:
            return
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, Message):
                        walk(child, depth + 1)
            return
        if part.get_content_disposition() == "attachment" or part.get_filename():
            return
        if part.get_content_type().lower() != "text/plain":
            return
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, TypeError):
            return
        if isinstance(content, str) and content:
            selected.append(content)

    walk(message, 0)
    return selected


def _has_pattern(patterns: tuple[re.Pattern[str], ...], content: str) -> bool:
    return any(pattern.search(content) is not None for pattern in patterns)


def classify_email_status(content: str, format: str = "text") -> dict[str, Any]:
    """Classify explicit email status signals without retaining message content.

    ``format`` accepts ``text`` or ``eml``. The EML parser reads only the subject
    and inline plain-text MIME parts; HTML, attachments, and address headers are
    ignored. The returned object contains no message-derived strings.
    """
    if format not in ("text", "eml"):
        raise ValueError("EMAIL_FORMAT_INVALID")
    if not isinstance(content, str):
        raise ValueError("EMAIL_CONTENT_INVALID")
    try:
        raw = content.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("EMAIL_CONTENT_INVALID") from exc
    if len(raw) > _MAX_CONTENT_BYTES:
        raise ValueError("EMAIL_CONTENT_TOO_LARGE")

    classification_text = content
    if format == "eml":
        try:
            message = Parser(policy=policy.default).parsestr(content, headersonly=False)
        except (ValueError, TypeError, UnicodeError):
            return {
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "category": "UNKNOWN",
                "suggested_status": None,
                "reason_codes": ["INVALID_FORMAT"],
            }
        subject = message.get("Subject", "")
        plain_parts = _walk_plain_text(message)
        # Bound the decoded text too, including parser expansion from transfer
        # encodings. These values remain local and are discarded on return.
        classification_text = "\n".join([str(subject), *plain_parts])
        if len(classification_text.encode("utf-8", errors="replace")) > _MAX_CONTENT_BYTES:
            classification_text = str(subject)[:_MAX_CONTENT_BYTES] + "\n" + "\n".join(plain_parts)
            classification_text = classification_text[:_MAX_CONTENT_BYTES]

    detected: list[tuple[str, str]] = []
    for category, status, patterns in _SIGNAL_PATTERNS:
        if _has_pattern(patterns, classification_text):
            detected.append((category, status))

    negated = _has_pattern(_NEGATED_PATTERNS, classification_text)
    conditional = _has_pattern(_CONDITIONAL_PATTERNS, classification_text)
    reasons: list[str] = []
    reasons.extend(_REASON_BY_CATEGORY[category] for category, _ in detected)

    if len(detected) > 1:
        category = "AMBIGUOUS"
        status = None
        reasons.append("CONFLICTING_SIGNALS")
    elif negated:
        category = "AMBIGUOUS"
        status = None
        reasons.append("NEGATED_SIGNAL")
    elif conditional:
        category = "AMBIGUOUS"
        status = None
        reasons.append("CONDITIONAL_SIGNAL")
    elif detected:
        category, status = detected[0]
    else:
        category = "UNKNOWN"
        status = None
        reasons.append("NO_EXPLICIT_SIGNAL")

    # Preserve stable codes, even if future regex groups share a reason.
    reasons = list(dict.fromkeys(reasons))
    return {
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "category": category,
        "suggested_status": status,
        "reason_codes": reasons,
    }
