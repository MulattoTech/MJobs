"""Build a deliberately lossy, schema-only HAR derivative without network access.

No source string is copied into the output. Hosts, path segments and field names
receive encounter-order aliases; values, cookies, headers and extensions are
discarded. This is analysis evidence, never a replayable or full-content HAR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit


VERSION = "1.0.0"
MAX_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 2000
MAX_BODY = 256 * 1024
MAX_FIELDS = 128
MAX_DEPTH = 8
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"})
MIMES = frozenset({"application/json", "application/x-www-form-urlencoded", "multipart/form-data",
                   "text/html", "text/plain", "text/css", "application/javascript", "text/javascript",
                   "image/png", "image/jpeg", "image/svg+xml", "image/webp", "application/pdf",
                   "application/octet-stream", "font/woff", "font/woff2"})
TIMINGS = ("blocked", "dns", "connect", "ssl", "send", "wait", "receive")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")


class HarError(ValueError):
    """Stable error codes only: never include input values, filenames or bodies."""


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: object, maximum: int = 86_400_000) -> int | float:
    if type(value) not in (int, float) or not -1 <= value <= maximum or not math.isfinite(value):
        return -1
    return value


def _mime(value: object) -> str:
    if not isinstance(value, str) or len(value) > 256:
        return "application/octet-stream"
    mime = value.split(";", 1)[0].strip().lower()
    if mime in MIMES:
        return mime
    if re.fullmatch(r"application/[a-z0-9.-]+\+json", mime):
        return "application/json"
    return "application/octet-stream"


class _Aliases:
    def __init__(self):
        self.groups: dict[str, dict[str, str]] = {}

    def get(self, group: str, raw: object) -> str:
        values = self.groups.setdefault(group, {})
        key = raw if isinstance(raw, str) else ""
        if key not in values:
            values[key] = f"{group}-{len(values) + 1}"
        return values[key]


def _shape(value: object, aliases: _Aliases, depth: int = 0) -> dict:
    if depth >= MAX_DEPTH:
        return {"type": "truncated"}
    if isinstance(value, dict):
        items = list(value.items())[:MAX_FIELDS]
        return {"type": "object", "properties": {
            aliases.get("field", key): _shape(item, aliases, depth + 1) for key, item in items
        }, "truncated": len(value) > MAX_FIELDS}
    if isinstance(value, list):
        shapes = [_shape(item, aliases, depth + 1) for item in value[:MAX_FIELDS]]
        unique = {json.dumps(item, sort_keys=True): item for item in shapes}
        return {"type": "array", "items": list(unique.values()), "truncated": len(value) > MAX_FIELDS}
    kind = "null" if value is None else "boolean" if isinstance(value, bool) else (
        "number" if isinstance(value, (int, float)) else "string")
    return {"type": kind}


def _body(value: dict, aliases: _Aliases) -> tuple[str, str]:
    if value.get("encoding"):
        return "", "omitted-encoded"
    text = value.get("text")
    if not isinstance(text, str):
        params = value.get("params")
        if isinstance(params, list):
            shape = {"type": "object", "properties": {
                aliases.get("field", _object(item).get("name")): {"type": "string"}
                for item in params[:MAX_FIELDS]
            }, "truncated": len(params) > MAX_FIELDS}
            return json.dumps(shape, separators=(",", ":")), "schema-only"
        return "", "omitted-unavailable"
    if len(text.encode("utf-8")) > MAX_BODY:
        return "", "omitted-size"
    mime = _mime(value.get("mimeType"))
    try:
        if mime == "application/json":
            parsed = json.loads(text)
        elif mime == "application/x-www-form-urlencoded":
            parsed = dict(parse_qsl(text, keep_blank_values=True, max_num_fields=MAX_FIELDS))
        else:
            return "", "omitted-unclassified"
        return json.dumps(_shape(parsed, aliases), separators=(",", ":")), "schema-only"
    except (ValueError, RecursionError, UnicodeError):
        return "", "omitted-invalid"


def _url(raw: object, aliases: _Aliases) -> tuple[str, list[dict]]:
    try:
        if not isinstance(raw, str) or len(raw) > 16384:
            raise ValueError
        parts = urlsplit(raw)
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
            raise ValueError
        origin = aliases.get("origin", f"{parts.scheme.lower()}://{parts.hostname.lower()}:{parts.port}")
        segments = parts.path.split("/")
        if len(segments) > MAX_FIELDS:
            raise ValueError
        route = "/".join(aliases.get("segment", segment) if segment else "" for segment in segments)
        fields = [{"name": aliases.get("field", key), "value": "[redacted]"}
                  for key, _ in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=MAX_FIELDS)]
        return f"https://{origin}.invalid/{route.lstrip('/')}", fields
    except (ValueError, UnicodeError):
        return "https://redacted.invalid/", []


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def sanitize_har(data: object) -> dict:
    """Reconstruct a HAR from safe types. Unknown source fields never survive."""
    log = _object(_object(data).get("log"))
    entries = log.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise HarError("INVALID_OR_OVERSIZED_HAR")
    if log.get("_mjobsSanitized"):
        raise HarError("ALREADY_SANITIZED")
    aliases = _Aliases()
    baseline = next((_date(_object(row).get("startedDateTime")) for row in entries
                     if _date(_object(row).get("startedDateTime")) is not None), EPOCH)

    def shifted(value: object) -> str:
        date = _date(value)
        delta = (date - baseline).total_seconds() if date else 0
        delta = min(max(delta, 0), 604800)
        return (EPOCH + timedelta(seconds=delta)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    result = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise HarError("INVALID_ENTRY")
        request, response = _object(raw.get("request")), _object(raw.get("response"))
        url, query = _url(request.get("url"), aliases)
        content = _object(response.get("content"))
        body, representation = _body(content, aliases)
        method = request.get("method")
        status = response.get("status")
        item = {
            "startedDateTime": shifted(raw.get("startedDateTime")), "time": _number(raw.get("time")),
            "request": {"method": method if isinstance(method, str) and method in METHODS else "UNKNOWN",
                        "url": url, "httpVersion": "", "headers": [], "cookies": [], "queryString": query,
                        "headersSize": -1, "bodySize": _number(request.get("bodySize"), 1_073_741_824)},
            "response": {"status": status if type(status) is int and 0 <= status <= 599 else 0,
                         "statusText": "", "httpVersion": "", "headers": [], "cookies": [],
                         "content": {"size": _number(content.get("size"), 1_073_741_824),
                                     "mimeType": "application/json" if body else _mime(content.get("mimeType")),
                                     "text": body, "_bodyRepresentation": representation,
                                     "_originalMimeType": _mime(content.get("mimeType"))},
                         "redirectURL": "", "headersSize": -1,
                         "bodySize": _number(response.get("bodySize"), 1_073_741_824)},
            "cache": {}, "timings": {key: _number(_object(raw.get("timings")).get(key)) for key in TIMINGS},
        }
        if "postData" in request:
            post = _object(request["postData"])
            text, mode = _body(post, aliases)
            item["request"]["postData"] = {"mimeType": "application/json" if text else _mime(post.get("mimeType")),
                                            "text": text, "_bodyRepresentation": mode,
                                            "_originalMimeType": _mime(post.get("mimeType"))}
        redirect = response.get("redirectURL")
        if isinstance(redirect, str) and redirect:
            item["response"]["redirectURL"], _ = _url(redirect, aliases)
        if isinstance(raw.get("pageref"), str):
            item["pageref"] = aliases.get("page", raw["pageref"])
        result.append(item)
    pages = log.get("pages", [])
    if not isinstance(pages, list) or len(pages) > MAX_ENTRIES:
        raise HarError("INVALID_PAGES")
    safe_pages = [{"id": aliases.get("page", _object(page).get("id")), "title": "[redacted]",
                   "startedDateTime": shifted(_object(page).get("startedDateTime")),
                   "pageTimings": {key: _number(_object(_object(page).get("pageTimings")).get(key))
                                   for key in ("onContentLoad", "onLoad")}} for page in pages]
    return {"log": {"version": "1.2", "creator": {"name": "MJobs HAR sanitizer", "version": VERSION},
                    "_mjobsSanitized": "schema-only", "_timestamps": "relative-shifted",
                    "pages": safe_pages, "entries": result}}


def validate_derivative(data: object) -> None:
    """Reject free-form strings, extensions and unsupported fields before sharing.

    This validates the closed representation; it cannot attest capture coverage
    or authorize publication. Manual context/privacy review remains required.
    """
    keys = {"log", "version", "creator", "name", "_mjobsSanitized", "_timestamps", "pages", "entries",
            "startedDateTime", "time", "request", "response", "method", "url", "httpVersion", "headers",
            "cookies", "queryString", "value", "headersSize", "bodySize", "status", "statusText", "content",
            "size", "mimeType", "text", "_bodyRepresentation", "_originalMimeType", "redirectURL", "cache",
            "timings", "postData", "pageref", "id", "title", "pageTimings", "onContentLoad", "onLoad",
            "type", "properties", "truncated", "items", *TIMINGS}
    literals = {"", "1.2", VERSION, "MJobs HAR sanitizer", "schema-only", "relative-shifted", "[redacted]",
                "omitted-encoded", "omitted-unavailable", "omitted-size", "omitted-unclassified", "omitted-invalid",
                "object", "array", "number", "boolean", "null", "string", "truncated", "UNKNOWN", *METHODS, *MIMES}

    def walk(value: object, depth: int = 0, key: str = "") -> None:
        if depth > 64:
            raise HarError("INVALID_DERIVATIVE")
        if isinstance(value, dict):
            for name, item in value.items():
                if name not in keys and not re.fullmatch(r"field-[1-9][0-9]*", name):
                    raise HarError("INVALID_DERIVATIVE")
                if name in {"headers", "cookies", "cache"} and item not in ([], {}):
                    raise HarError("INVALID_DERIVATIVE")
                walk(item, depth + 1, name)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1, key)
        elif isinstance(value, str):
            if key == "text" and value:
                try:
                    shape = json.loads(value)
                except (ValueError, RecursionError):
                    raise HarError("INVALID_DERIVATIVE") from None
                if not isinstance(shape, dict) or "type" not in shape:
                    raise HarError("INVALID_DERIVATIVE")
                walk(shape, depth + 1)
            elif value not in literals and not re.fullmatch(r"(?:field|page)-[1-9][0-9]*", value) and not (
                re.fullmatch(r"https://(?:origin-[1-9][0-9]*|redacted)\.invalid/(?:segment-[1-9][0-9]*|/)*", value)
                or re.fullmatch(r"2000-01-0[1-8]T\d{2}:\d{2}:\d{2}\.\d{3}Z", value)
            ):
                raise HarError("INVALID_DERIVATIVE")
        elif type(value) in (int, float):
            if _number(value, 1_073_741_824) != value:
                raise HarError("INVALID_DERIVATIVE")
        elif not isinstance(value, bool):
            raise HarError("INVALID_DERIVATIVE")

    log = _object(_object(data).get("log"))
    if set(_object(data)) != {"log"} or log.get("_mjobsSanitized") != "schema-only":
        raise HarError("INVALID_DERIVATIVE")
    walk(data)


def sanitize_bytes(raw: bytes) -> tuple[bytes, dict]:
    if len(raw) > MAX_BYTES:
        raise HarError("INPUT_TOO_LARGE")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        clean = sanitize_har(data)
        validate_derivative(clean)
        output = (json.dumps(clean, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(output) > MAX_OUTPUT_BYTES:
            raise HarError("OUTPUT_TOO_LARGE")
    except HarError:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise HarError("INVALID_INPUT") from None
    manifest = {"schema_version": 1, "sanitizer_version": VERSION, "source_sha256": hashlib.sha256(raw).hexdigest(),
                "derivative_sha256": hashlib.sha256(output).hexdigest(), "source_bytes": len(raw),
                "derivative_bytes": len(output), "entries": len(clean["log"]["entries"]),
                "capture_status": "NOT_ATTESTED", "representation": "schema-only",
                "raw_retention": "CALLER_MANAGED_PRIVATE_STORAGE", "privacy_review": "REQUIRED_BEFORE_SHARING",
                "closed_representation_validation": "PASS",
                "policy": "allowlisted-types-no-source-strings-v1"}
    return output, manifest


def write_derivative(source: Path, output: Path, manifest_path: Path) -> dict:
    paths = [path.resolve() for path in (source, output, manifest_path)]
    if len(set(paths)) != 3:
        raise HarError("PATH_ALIAS_REFUSED")
    with source.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    clean, manifest = sanitize_bytes(raw)
    payloads = [(output, clean), (manifest_path, (json.dumps(manifest, indent=2) + "\n").encode())]
    temporaries, published = [], []
    try:
        for target, payload in payloads:
            descriptor, temporary = tempfile.mkstemp(prefix=".har-sanitized-", dir=target.parent)
            temporaries.append(Path(temporary))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Exclusive atomic publication: never replace another run's artifact.
            os.link(temporary, target)
            published.append(target)
    except OSError:
        for target in published:
            target.unlink(missing_ok=True)
        raise HarError("OUTPUT_PUBLICATION_FAILED") from None
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = write_derivative(args.source, args.output, args.manifest)
    except (HarError, OSError):
        # Suppress raw JSON errors, OS paths and any captured secret text.
        print("HAR_SANITIZATION_FAILED", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
