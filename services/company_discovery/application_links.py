"""Bounded, evidence-only checks for sampled public application links."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json

from services.company_discovery.public_client import HTTPStatusError, PublicFetchError
from services.discovery.feed_items import describe_feed_item, plain_text
from services.company_discovery.public_client import _parse_url


MAX_SAMPLE = 5
MAX_JSONLD_SCRIPTS = 20
MAX_SCRIPT_BYTES = 100 * 1024
MAX_JSONLD_NODES = 128
MAX_JSONLD_DEPTH = 8


@dataclass(frozen=True)
class LinkReport:
    external_key: str
    raw_sha256: str
    requested_url: str
    endpoint: str
    observed_at: str
    final_url: str | None
    result: str
    reason: str
    response_status: int | None
    response_sha256: str | None
    identity_evidence: tuple[str, ...]


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self._active = False
        self._is_jsonld = False
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "script" or self._active:
            return
        self._active = True
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        self._is_jsonld = values.get("type", "").split(";", 1)[0].strip().casefold() == "application/ld+json"
        self._parts = []

    def handle_data(self, data):
        if self._active and self._is_jsonld:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.casefold() == "script" and self._active:
            if self._is_jsonld:
                self.scripts.append("".join(self._parts))
            self._active = False
            self._is_jsonld = False
            self._parts = []


def _time(value):
    if value is None:
        result = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("APPLICATION_LINK_TIME_INVALID")
    return result.astimezone(timezone.utc)


def _origin(value):
    parsed = _parse_url(str(value or ""))
    if parsed is None:
        return None
    _, host, port = parsed
    return "https", host, port


def _jsonld_postings(body):
    parser = _Scripts()
    parser.feed(body)
    if len(parser.scripts) > MAX_JSONLD_SCRIPTS:
        return None
    nodes = []
    count = 0

    def visit(value, depth=0):
        nonlocal count
        count += 1
        if count > MAX_JSONLD_NODES or depth > MAX_JSONLD_DEPTH:
            raise ValueError
        if isinstance(value, dict):
            types = value.get("@type")
            job_types = {'JobPosting', 'https://schema.org/JobPosting', 'http://schema.org/JobPosting'}
            if (isinstance(types, str) and types in job_types
                    or isinstance(types, list) and any(isinstance(value, str) and value in job_types for value in types)):
                nodes.append(value)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)

    def loads(value):
        pairs = []
        parsed = json.loads(value, object_pairs_hook=lambda entries: _unique_object(entries, pairs))
        if pairs:
            raise ValueError
        return parsed

    try:
        for script in parser.scripts:
            if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
                return None
            visit(loads(script), 0)
        if parser._active:
            return None
    except (ValueError, TypeError, json.JSONDecodeError, RecursionError):
        return None
    return nodes


def _unique_object(entries, duplicate_marker):
    result = {}
    for key, value in entries:
        if key in result:
            duplicate_marker.append(key)
        result[key] = value
    return result


def _identifier(posting):
    value = posting.get("identifier")
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value) if str(value).strip() else None
    return None


def _title(value):
    return plain_text(value).casefold()


def _report(feed, endpoint, observed, **values):
    return LinkReport(external_key=feed.external_key, raw_sha256=feed.raw_sha256,
                      requested_url=feed.application_url, endpoint=endpoint,
                      observed_at=observed, **values)


def _evidence(*extra):
    return ("Sample selection: UTC day plus external key plus raw SHA-256, maximum five rows.",
            "Identity evidence is declared JobPosting identity only; it is not application or employer verification.",
            *extra)


def sample_feed_links(items, strategy, endpoint, client, *, at=None, check=None):
    """Sample at most five accepted feed identities through one bounded client."""
    check = check or (lambda: None)
    reference = _time(at)
    fixed_observed = reference.isoformat() if at is not None else None
    endpoint_origin = _origin(endpoint)
    if endpoint_origin is None:
        raise ValueError("APPLICATION_LINK_ENDPOINT_INVALID")
    accepted = {}
    for item in items or []:
        feed = describe_feed_item(item, strategy, endpoint)
        if feed is None:
            continue
        prior = accepted.get(feed.external_key)
        if prior is not None and prior.raw_sha256 != feed.raw_sha256:
            raise ValueError("SOURCE_DUPLICATE_JOB_ID")
        accepted[feed.external_key] = feed
    selected = sorted(accepted.values(), key=lambda feed: hashlib.sha256(
        f"{reference.date().isoformat()}:{feed.external_key}:{feed.raw_sha256}".encode()).hexdigest())[:MAX_SAMPLE]
    reports = []
    halted = None
    for feed in selected:
        observed = fixed_observed or _time(None).isoformat()
        check()
        if halted:
            reports.append(_report(feed, endpoint, observed, final_url=None, result="UNKNOWN", reason=halted,
                                   response_status=None, response_sha256=None, identity_evidence=_evidence()))
            continue
        if _origin(feed.application_url) != endpoint_origin:
            reports.append(_report(feed, endpoint, observed, final_url=None, result="UNKNOWN",
                                   reason="CROSS_ORIGIN", response_status=None, response_sha256=None,
                                   identity_evidence=_evidence("Requested URL is outside the enrolled HTTPS origin.")))
            continue
        try:
            check()
            response = client.get(feed.application_url)
        except HTTPStatusError as error:
            observed = fixed_observed or _time(None).isoformat()
            status = getattr(error, "status_code", None)
            result = "INACCURATE" if status in (404, 410) else "UNKNOWN"
            reason = f"HTTP_{status}" if status in (404, 410) else "PUBLIC_FETCH_BLOCKED"
            reports.append(_report(feed, endpoint, observed, final_url=getattr(error, "final_url", None),
                                   result=result, reason=reason, response_status=status,
                                   response_sha256=getattr(error, "response_sha256", None), identity_evidence=_evidence()))
            if status == 429 or getattr(error, "code", "") in {"REQUEST_BUDGET", "FETCH_DEADLINE", "HTTP_STATUS_BLOCKED"}:
                halted = "PUBLIC_FETCH_BLOCKED"
            continue
        except PublicFetchError as error:
            observed = fixed_observed or _time(None).isoformat()
            reports.append(_report(feed, endpoint, observed, final_url=None, result="UNKNOWN",
                                   reason="PUBLIC_FETCH_BLOCKED", response_status=None, response_sha256=None,
                                   identity_evidence=_evidence()))
            if getattr(error, "code", "") in {"REQUEST_BUDGET", "FETCH_DEADLINE"}:
                halted = "PUBLIC_FETCH_BLOCKED"
            continue
        observed = fixed_observed or _time(None).isoformat()
        final_url = str(getattr(response, "url", "") or "")
        if not final_url:
            reports.append(_report(feed, endpoint, observed, final_url=None, result="UNKNOWN",
                                   reason="RESPONSE_URL_MISSING", response_status=getattr(response, "status_code", None),
                                   response_sha256=None, identity_evidence=_evidence()))
            continue
        response_hash = hashlib.sha256(bytes(getattr(response, "content", b""))).hexdigest()
        status = getattr(response, "status_code", None)
        if status in (404, 410):
            reports.append(_report(feed, endpoint, observed, final_url=final_url, result="INACCURATE",
                                   reason=f"HTTP_{status}", response_status=status,
                                   response_sha256=response_hash, identity_evidence=_evidence()))
            continue
        if status != 200:
            reports.append(_report(feed, endpoint, observed, final_url=final_url, result="UNKNOWN",
                                   reason="HTTP_STATUS_UNKNOWN", response_status=status,
                                   response_sha256=response_hash, identity_evidence=_evidence()))
            continue
        if _origin(final_url) != endpoint_origin:
            reports.append(_report(feed, endpoint, observed, final_url=final_url, result="UNKNOWN",
                                   reason="CROSS_ORIGIN", response_status=getattr(response, "status_code", None),
                                   response_sha256=response_hash, identity_evidence=_evidence()))
            continue
        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().casefold()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            reports.append(_report(feed, endpoint, observed, final_url=final_url, result="UNKNOWN",
                                   reason="CONTENT_TYPE_UNSUPPORTED", response_status=status,
                                   response_sha256=response_hash, identity_evidence=_evidence()))
            continue
        content = bytes(getattr(response, "content", b""))
        if len(content) > 2 * 1024 * 1024:
            reports.append(_report(feed, endpoint, observed, final_url=final_url, result="UNKNOWN",
                                   reason="RESPONSE_TOO_LARGE", response_status=status,
                                   response_sha256=response_hash, identity_evidence=_evidence()))
            continue
        postings = _jsonld_postings(content.decode("utf-8", errors="replace"))
        matches = [posting for posting in (postings or []) if _identifier(posting) == feed.external_key]
        declared = [posting for posting in matches if _title(posting.get("title")) == _title(feed.title)
                    and isinstance(posting.get("url"), str) and posting["url"] in {feed.application_url, final_url}]
        if len(postings or []) != 1:
            reason = "JSONLD_IDENTITY_AMBIGUOUS" if len(postings or []) > 1 else "JSONLD_IDENTITY_MISSING"
            result = "UNKNOWN"
            evidence = _evidence()
        elif len(declared) == 1:
            reason = "JSONLD_IDENTITY_MATCH"
            result = "ACCURATE"
            evidence = _evidence("Exactly one JSON-LD JobPosting matched the feed external key.",
                                 "Normalized title and declared absolute URL matched the requested or final URL.")
        else:
            reason = "JSONLD_URL_MISMATCH" if matches else "JSONLD_IDENTITY_MISSING"
            result = "UNKNOWN"
            evidence = _evidence()
        reports.append(_report(feed, endpoint, observed, final_url=final_url, result=result, reason=reason,
                               response_status=getattr(response, "status_code", None), response_sha256=response_hash,
                               identity_evidence=evidence))
    return [replace(report, identity_evidence=(*report.identity_evidence,
            "Sampling UTC day: " + reference.date().isoformat())) for report in reports]
