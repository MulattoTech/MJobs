"""Bounded, deterministic job-field extraction from supplied local capture data."""
from __future__ import annotations

from collections.abc import Mapping
from html import unescape
import json
import re
from urllib.parse import urlsplit


MAX_SCHEMA_JSON_CHARS = 100_000
MAX_CAPTURE_TEXT_CHARS = 20_000
MAX_PROVIDER_FIELDS = 100
MAX_RAW_CAPTURE_REF_CHARS = 256

FIELD_NAMES = (
    "title", "company", "description", "location", "employment_type",
    "date_posted", "valid_through", "source_job_id", "listing_url",
    "application_url",
)
_IDENTITY_AND_ROUTE_FIELDS = {
    "title", "company", "source_job_id", "listing_url", "application_url",
}
_LABELS = {
    "title": "title",
    "job title": "title",
    "company": "company",
    "company name": "company",
    "employer": "company",
    "description": "description",
    "job description": "description",
    "location": "location",
    "employment type": "employment_type",
    "employment": "employment_type",
    "date posted": "date_posted",
    "posted": "date_posted",
    "valid through": "valid_through",
    "closing date": "valid_through",
    "job id": "source_job_id",
    "job identifier": "source_job_id",
    "listing url": "listing_url",
    "job url": "listing_url",
    "application url": "application_url",
    "apply url": "application_url",
}
_LABEL_LINE = re.compile(r"^\s*([^:]{1,64})\s*:\s*(.*?)\s*$")
_OPAQUE_CAPTURE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAGS = re.compile(r"<[^>]{0,1000}>")


def _plain_text(value):
    if not isinstance(value, str):
        return None
    text = value[:MAX_CAPTURE_TEXT_CHARS]
    if "<" in text and ">" in text:
        text = _SCRIPT_STYLE.sub(" ", text)
        text = _TAGS.sub(" ", text)
    text = unescape(text)
    text = " ".join(text.split())
    return text or None


def _scalar(value, field):
    if field == "description":
        return _plain_text(value)
    if field == "employment_type" and isinstance(value, (list, tuple)):
        if len(value) > 10 or any(not isinstance(item, str) for item in value):
            return None
        value = ", ".join(value)
    if field == "source_job_id" and type(value) is int:
        value = str(value)
    if not isinstance(value, str):
        return None
    value = " ".join(value[:MAX_CAPTURE_TEXT_CHARS].split())
    if not value:
        return None
    if field in {"listing_url", "application_url"}:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or (port is not None and not 1 <= port <= 65535)):
            return None
    return value


def _path(value, dotted_path):
    current = value
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _first_path(value, paths):
    for path in paths:
        item = _path(value, path)
        if item is not None:
            return item
    return None


_PROVIDER_PATHS = {
    "greenhouse": {
        "title": ("title",),
        "company": ("company_name", "company.name"),
        "description": ("content", "description"),
        "location": ("location.name", "location"),
        "employment_type": ("employment_type",),
        "date_posted": ("date_posted", "first_published_at"),
        "valid_through": ("valid_through",),
        "source_job_id": ("id", "internal_job_id"),
        "listing_url": ("absolute_url", "url"),
        "application_url": ("application_url", "apply_url"),
    },
    "lever": {
        "title": ("text", "title"),
        "company": ("company", "company_name"),
        "description": ("descriptionPlain", "description"),
        "location": ("categories.location", "location"),
        "employment_type": ("categories.commitment", "employment_type"),
        "date_posted": ("datePosted", "date_posted"),
        "valid_through": ("validThrough", "valid_through"),
        "source_job_id": ("id",),
        "listing_url": ("hostedUrl", "listing_url"),
        "application_url": ("applyUrl", "application_url"),
    },
    "workday": {
        "title": ("jobPostingInfo.jobTitle", "jobTitle"),
        "company": ("jobPostingInfo.companyName", "companyName"),
        "description": ("jobPostingInfo.jobDescription", "jobDescription"),
        "location": ("jobPostingInfo.location", "location"),
        "employment_type": ("jobPostingInfo.timeType", "timeType"),
        "date_posted": ("jobPostingInfo.postingDate", "postingDate"),
        "valid_through": ("jobPostingInfo.validThrough", "validThrough"),
        "source_job_id": ("jobPostingInfo.jobReqId", "jobReqId"),
        "listing_url": ("jobPostingInfo.externalUrl", "externalUrl"),
        "application_url": ("jobPostingInfo.applicationUrl", "applicationUrl"),
    },
    "remoteok": {
        "title": ("position", "title"),
        "company": ("company",),
        "description": ("description",),
        "location": ("location",),
        "employment_type": ("employment_type",),
        "date_posted": ("date", "date_posted"),
        "valid_through": ("valid_through",),
        "source_job_id": ("id",),
        "listing_url": ("url",),
        "application_url": ("apply_url", "application_url"),
    },
}


def _job_posting_mapping(value):
    if isinstance(value, str):
        if len(value) > MAX_SCHEMA_JSON_CHARS:
            return None
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(value, Mapping):
        return None
    if len(value) > MAX_PROVIDER_FIELDS:
        return None
    graph = value.get("@graph")
    candidates = graph[:MAX_PROVIDER_FIELDS] if isinstance(graph, list) else [value]
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        kind = candidate.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(isinstance(item, str) and item.rstrip("/").casefold() in {
                "jobposting", "https://schema.org/jobposting", "http://schema.org/jobposting",
        } for item in kinds):
            return candidate
    return None


def _schema_fields(value):
    posting = _job_posting_mapping(value)
    if posting is None:
        return {}
    organization = posting.get("hiringOrganization")
    company = organization.get("name") if isinstance(organization, Mapping) else organization
    identifier = posting.get("identifier")
    if isinstance(identifier, Mapping):
        identifier = identifier.get("value") or identifier.get("name")
    location_value = posting.get("jobLocation")
    locations = location_value if isinstance(location_value, list) else [location_value]
    locations_text = []
    for row in locations[:10]:
        if isinstance(row, Mapping):
            address = row.get("address")
            if isinstance(address, Mapping):
                parts = [address.get(key) for key in (
                    "streetAddress", "addressLocality", "addressRegion", "postalCode",
                    "addressCountry",
                )]
                location = ", ".join(value for value in parts if isinstance(value, str) and value.strip())
            else:
                location = row.get("name")
        else:
            location = row
        if isinstance(location, str) and location.strip() and location not in locations_text:
            locations_text.append(location)
    return {
        "title": posting.get("title"),
        "company": company,
        "description": posting.get("description"),
        "location": "; ".join(locations_text) if locations_text else None,
        "employment_type": posting.get("employmentType"),
        "date_posted": posting.get("datePosted"),
        "valid_through": posting.get("validThrough"),
        "source_job_id": identifier,
        "listing_url": posting.get("url"),
        "application_url": posting.get("applicationUrl") or posting.get("application_url"),
    }


def _provider_fields(provider, value):
    if not isinstance(provider, str) or not isinstance(value, Mapping) or len(value) > MAX_PROVIDER_FIELDS:
        return {}
    fields = _PROVIDER_PATHS.get(provider.strip().casefold())
    if fields is None:
        return {}
    return {field: _first_path(value, paths) for field, paths in fields.items()}


def _text_fields(value, *, source, confidence, unlabelled):
    if not isinstance(value, str):
        return {}
    text = value[:MAX_CAPTURE_TEXT_CHARS]
    lines = text.splitlines()
    extracted = {}
    active_description = False
    description_lines = None
    for line in lines:
        match = _LABEL_LINE.match(line)
        field = _LABELS.get(match.group(1).strip().casefold()) if match else None
        if field:
            active_description = field == "description"
            field_value = match.group(2) if match else ""
            if field == "description":
                description_lines = [field_value] if field_value else []
                continue
            normalized = _scalar(field_value, field)
            if normalized:
                extracted[field] = normalized
            continue
        if active_description:
            if description_lines is None:
                description_lines = []
            description_lines.append(line.strip())
    if description_lines is not None:
        description = _plain_text("\n".join(description_lines))
        if description:
            extracted["description"] = description
    if not extracted and unlabelled:
        content = _plain_text(text)
        first_line = next((row.strip() for row in lines if row.strip()), None)
        title = _scalar(first_line, "title") if first_line else None
        if title:
            extracted["title"] = title
        if content:
            extracted["description"] = content
    if not extracted:
        return {}
    return {
        field: {"value": value, "source": source, "confidence": confidence}
        for field, value in extracted.items()
    }


def _source_fields(raw_fields, source, confidence):
    result = {}
    if not isinstance(raw_fields, Mapping):
        return result
    for field in FIELD_NAMES:
        value = _scalar(raw_fields.get(field), field)
        if value is not None:
            result[field] = {"value": value, "source": source, "confidence": confidence}
    return result


def extract_job_capture(capture):
    """Extract bounded job fields from capture data already supplied by the caller.

    No page is fetched, browser is launched, capture is persisted, or instruction
    in captured content is acted upon. Only the opaque capture reference is
    returned; raw page data and unrelated fields are deliberately not copied.
    """
    if not isinstance(capture, Mapping):
        raise ValueError("Capture must be a mapping")
    if len(capture) > 100:
        raise ValueError("Capture contains too many fields")
    raw_capture_ref = capture.get("raw_capture_ref")
    if raw_capture_ref is not None and (
            not isinstance(raw_capture_ref, str) or not raw_capture_ref.strip()
            or raw_capture_ref != raw_capture_ref.strip()
            or len(raw_capture_ref) > MAX_RAW_CAPTURE_REF_CHARS
            or not _OPAQUE_CAPTURE_REF.fullmatch(raw_capture_ref)
            or any(ord(character) < 32 for character in raw_capture_ref)):
        raise ValueError("Raw capture reference must be a bounded opaque string")

    candidates = (
        _source_fields(_schema_fields(capture.get("job_posting")), "JOBPOSTING_SCHEMA", 0.98),
        _source_fields(_provider_fields(capture.get("provider"), capture.get("provider_fields")),
                       "PROVIDER_FIELDS", 0.90),
        _text_fields(capture.get("selected_text"), source="USER_SELECTED_TEXT",
                     confidence=0.82, unlabelled=True),
        _text_fields(capture.get("visible_text"), source="VISIBLE_TEXT",
                     confidence=0.65, unlabelled=True),
    )
    selected = {}
    for candidate_set in candidates:
        for field, value in candidate_set.items():
            selected.setdefault(field, value)

    fields = {}
    review_required = []
    for name in FIELD_NAMES:
        value = selected.get(name)
        if value is None:
            fields[name] = {"value": None, "source": "UNKNOWN", "confidence": 0.0,
                            "review_required": False}
            continue
        needs_review = (name in _IDENTITY_AND_ROUTE_FIELDS and value["confidence"] < 0.8)
        fields[name] = {**value, "review_required": needs_review}
        if needs_review:
            review_required.append(name)
    return {
        "fields": fields,
        "raw_capture_ref": raw_capture_ref,
        "review_required": review_required,
    }
