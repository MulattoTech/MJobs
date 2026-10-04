"""Conservative redaction before AFK state or artifact writes."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", re.I)
BEARER = re.compile(r"\bBearer\s+[^\s,;]+", re.I)
COOKIE = re.compile(r"\b(?:set-cookie|cookie)\s*:\s*[^\r\n]+", re.I)
ASSIGNMENT = re.compile(
    r"\b(password|passwd|pwd|api[_-]?key|secret|access[_-]?token|refresh[_-]?token|session[_-]?token|client[_-]?secret|dpapi[_-]?(?:blob|data))\b(\s*[:=]\s*)([\"']?)[^\s,;\"']+\3",
    re.I,
)
JSON_SECRET = re.compile(r'(?i)("(?:password|passwd|pwd|api[_-]?key|secret|access[_-]?token|refresh[_-]?token|session[_-]?token|client[_-]?secret|cookie|authorization|dpapi[_-]?(?:blob|data))"\s*:\s*)"(?:\\.|[^"\\])*"')
TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{12,})\b")
URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.I)
SENSITIVE_KEY = re.compile(r"password|passwd|pwd|secret|token|cookie|authorization|private.?key|dpapi", re.I)


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    suffix = ""
    while raw and raw[-1] in ".,);":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    parts = urlsplit(raw)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "[REDACTED]@" + netloc.rsplit("@", 1)[1]
    query = re.sub(r"(?i)(token|key|secret|password|code|session)=([^&]+)", r"\1=[REDACTED]", parts.query)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment)) + suffix


def redact(value: str) -> str:
    text = PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", str(value))
    text = JSON_SECRET.sub(lambda m: m.group(1) + '"[REDACTED]"', text)
    text = URL.sub(_redact_url, text)
    text = BEARER.sub("Bearer [REDACTED]", text)
    text = COOKIE.sub(lambda m: m.group(0).split(":", 1)[0] + ": [REDACTED]", text)
    text = ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)
    text = TOKEN.sub("[REDACTED_TOKEN]", text)
    return text


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SENSITIVE_KEY.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value
