"""Bounded, read-only public metadata projections over durable source postings.

The original payload remains the storage authority. Projection does not verify a
source's claims, infer field confidence, or change canonical job values.
"""
from __future__ import annotations

import json
import math
import re
from html import unescape
from urllib.parse import unquote, urlsplit


SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 256 * 1024
MAX_STRING_BYTES = 64 * 1024
MAX_KEY_BYTES = 256
MAX_DEPTH = 12
MAX_NODES = 4096
MAX_COLLECTION_ITEMS = 512
MAX_INTEGER_BITS = 4096

_PRIVATE_PARTS = (
    "password", "passwd", "credential", "secret", "token", "cookie", "session",
    "diagnostic", "trace", "authorization", "authentication", "privatekey",
    "apikey", "accesskey", "encryptionkey", "signingkey", "generationcontext",
    "prompt", "stderr", "stdout", "browser",
)
_PRIVATE_KEYS = frozenset({
    "auth", "bearer", "headers", "request", "response", "error", "exception",
    "profile", "candidate", "contact", "answers", "answer", "submittedfields",
    "resume", "coverletter", "raw", "rawpayload", "rawstructuredpayload",
})
_SOURCE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:\-]{1,200}\Z")
_URL_TOKEN = re.compile(
    r"(?:[a-z][a-z0-9+.\-]{0,31}://|(?:data|javascript|mailto|file):|//)[^\s<>\"']*", re.I,
)
_OMIT = object()


class _InvalidMetadata(ValueError):
    pass


def _private_key(value: str) -> bool:
    folded = re.sub(r"[^a-z0-9]", "", value.casefold())
    return folded in _PRIVATE_KEYS or any(part in folded for part in _PRIVATE_PARTS)


def _public_url(value):
    """Return an unchanged public URL, or omit it without exposing a partial link."""
    if type(value) is not str or not value or len(value) > 4096:
        return None
    if "\\" in value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            return None
        # Reading .port also rejects malformed authorities.
        _ = parsed.port
        decoded_path = parsed.path
        for _ in range(3):
            decoded = unquote(decoded_path)
            if decoded == decoded_path:
                break
            decoded_path = decoded
        if any(_private_key(segment) for segment in decoded_path.split("/") if segment):
            return None
        value.encode("utf-8")
        return value
    except (UnicodeError, ValueError):
        return None


def _source_text(value, *, maximum=200):
    if type(value) is not str or not value or len(value) > maximum:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    if _has_unsafe_url(value):
        return None
    return value


def _has_unsafe_url(value):
    # The scheme prefix is bounded so URL-free strings scan in linear time.
    return any(_public_url(match.group()) is None for match in _URL_TOKEN.finditer(unescape(value)))


def _source_context(row):
    source = row.get("source")
    source_identifier = _source_text(source)
    if source_identifier is not None and not _SOURCE_IDENTIFIER.fullmatch(source_identifier):
        source_identifier = None
    source_url = row.get("source_url") or (source if type(source) is str and _URL_TOKEN.search(source) else None)
    safe_url = _public_url(source_url)
    context = {
        "posting_id": _source_text(row.get("id"), maximum=128),
        "source_job_id": _source_text(row.get("source_job_id"), maximum=128),
        "source_identifier": source_identifier,
        "source_url": safe_url,
        "fetched_at": _source_text(row.get("fetched_at"), maximum=64),
    }
    omitted_identity = sum(
        row.get(original) is not None and row.get(original) != "" and context[projected] is None
        for original, projected in (("id", "posting_id"), ("source_job_id", "source_job_id"),
                                    ("fetched_at", "fetched_at"))
    )
    # URL-valued source records use source_url rather than a source identifier.
    if source is not None and source != "" and source_identifier is None and _public_url(source) is None:
        omitted_identity += 1
    return context, bool(source_url and safe_url is None), omitted_identity


def _project(value, state, depth=0):
    state["nodes"] += 1
    if depth > MAX_DEPTH or state["nodes"] > MAX_NODES:
        raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > MAX_INTEGER_BITS:
            raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _InvalidMetadata("INVALID_METADATA_VALUE")
        return value
    if type(value) is str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise _InvalidMetadata("INVALID_METADATA_VALUE") from None
        if size > MAX_STRING_BYTES:
            raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
        state["bytes"] += size
        if state["bytes"] > MAX_METADATA_BYTES:
            raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
        # Source strings may contain a link in prose or HTML rather than start
        # with it. Do not export known capability URLs hidden in that wrapper.
        if _has_unsafe_url(value):
            state["unsafe_urls"] += 1
            return _OMIT
        return value
    if type(value) is list:
        if len(value) > MAX_COLLECTION_ITEMS:
            raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
        result = []
        for item in value:
            projected = _project(item, state, depth + 1)
            # Keep list positions; an omitted URL is explicit in envelope counts.
            result.append(None if projected is _OMIT else projected)
        return result
    if type(value) is dict:
        if len(value) > MAX_COLLECTION_ITEMS:
            raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _InvalidMetadata("INVALID_METADATA_KEY")
            try:
                key_bytes = len(key.encode("utf-8"))
            except UnicodeError:
                raise _InvalidMetadata("INVALID_METADATA_KEY") from None
            if key_bytes > MAX_KEY_BYTES:
                raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
            if any(ord(char) < 32 or ord(char) == 127 for char in key):
                raise _InvalidMetadata("INVALID_METADATA_KEY")
            state["nodes"] += 1
            state["bytes"] += key_bytes
            if state["nodes"] > MAX_NODES or state["bytes"] > MAX_METADATA_BYTES:
                raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
            if _private_key(key):
                state["private_fields"] += 1
                continue
            if _has_unsafe_url(key):
                state["unsafe_urls"] += 1
                continue
            projected = _project(item, state, depth + 1)
            if projected is not _OMIT:
                result[key] = projected
        return result
    raise _InvalidMetadata("INVALID_METADATA_VALUE")


def source_metadata_view(row):
    """Project one persisted posting without mutating its payload or authority.

COMPLETE means structurally complete under this projection policy, not verified
or guaranteed free of all sensitive prose. PARTIAL names omissions. UNAVAILABLE
retains source identity but returns no truncated subset of invalid/large data.
"""
    source, unsafe_source_url, omitted_identity = _source_context(row)
    state = {"nodes": 0, "bytes": 0, "private_fields": 0, "unsafe_urls": int(unsafe_source_url)}
    reason_codes = []
    extensions = {}
    status = "COMPLETE"
    try:
        raw = row.get("raw")
        if type(raw) is not dict:
            raise _InvalidMetadata("SOURCE_METADATA_NOT_OBJECT")
        extensions = _project(raw, state)
        encoded = json.dumps(extensions, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise _InvalidMetadata("METADATA_LIMIT_EXCEEDED")
    except _InvalidMetadata as error:
        extensions = {}
        status = "UNAVAILABLE"
        reason_codes.append(str(error))
    if state["private_fields"]:
        reason_codes.append("PRIVATE_FIELDS_OMITTED")
    if state["unsafe_urls"]:
        reason_codes.append("UNSAFE_URLS_OMITTED")
    if omitted_identity:
        reason_codes.append("SOURCE_IDENTITY_OMITTED")
    if reason_codes and status != "UNAVAILABLE":
        status = "PARTIAL"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "source": source,
        "extensions": extensions,
        "omissions": {"private_fields": state["private_fields"], "unsafe_urls": state["unsafe_urls"],
                      "source_identity_fields": omitted_identity},
        "reason_codes": sorted(reason_codes),
    }
