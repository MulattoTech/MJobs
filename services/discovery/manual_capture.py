"""Persist user-entered jobs through the canonical job, route, and posting model."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from html import unescape
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.discovery.sources.collect import persist_posting
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.requirement_parsers import parse_certifications, parse_skill_requirements


class ManualCaptureConflict(ValueError):
    """An idempotency key was reused for different user-entered content."""


_EXTRACTION_FIELDS = (
    "title", "company", "description", "location", "employment_type",
    "date_posted", "valid_through", "source_job_id", "listing_url",
    "application_url",
)
_EXTRACTION_SOURCES = {
    "JOBPOSTING_SCHEMA", "PROVIDER_FIELDS", "USER_SELECTED_TEXT", "VISIBLE_TEXT", "UNKNOWN",
}
_LOW_CONFIDENCE_IDENTITY_FIELDS = {
    "title", "company", "source_job_id", "listing_url", "application_url",
}
_EXTRACTION_FIELD_LIMITS = {
    "title": 500, "company": 200, "description": 20_000, "location": 1000,
    "employment_type": 500, "date_posted": 100, "valid_through": 100,
    "source_job_id": 256, "listing_url": 2000, "application_url": 2000,
}
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?i)(token|session|auth|password|secret|credential|access_key|api_key|authorization|"
    r"cookie|jwt|csrf|xsrf|signature|(^|_)sig($|_)|security_token)"
)
_SENSITIVE_PATH_SEGMENTS = {
    "token", "session", "auth", "password", "secret", "credential", "access_key",
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|token|session|password|secret|credential|"
    r"auth(?:orization)?|api[_-]?key|cookie|jwt|csrf|xsrf|signature|sig)\b\s*[:=]\s*)[^\s,;&]+"
)
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_HTML_TAG = re.compile(r"<[^>]{0,1000}>")
_OPAQUE_CAPTURE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SENSITIVE_CAPTURE_REF = re.compile(
    r"(?i)(?:^|[-_.:])(token|session|password|secret|cookie|credential)(?:$|[-_.:])"
)
_RAW_CAPTURE_INPUT_KEY = re.compile(
    r"(?i)(?:^|_)(raw.?html|html|page.?html|page.?text|visible.?text|selected.?text|"
    r"provider.?fields|job.?posting|browser.?capture|raw.?capture|capture.?content|cookie|"
    r"form.?answers?|instructions?)(?:$|_)"
)


def _plain_capture_text(value: str, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or len(value) > max_chars:
        raise ValueError(f"Extraction capture {field} is invalid")
    text = value
    for _ in range(3):
        decoded = unescape(text)
        if decoded == text:
            break
        text = decoded
    if "<" in text and ">" in text:
        text = _SCRIPT_STYLE.sub(" ", text)
        text = _HTML_TAG.sub(" ", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return " ".join(text.split())


def _url(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise ValueError(f"{field} must be a complete HTTP or HTTPS URL")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        port = parsed.port
        if (scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"{field} must be a complete HTTP or HTTPS URL") from error
    if not host:
        raise ValueError(f"{field} must be a complete HTTP or HTTPS URL")
    if port == (80 if scheme == "http" else 443):
        port = None
    authority_host = f"[{host}]" if ":" in host else host
    netloc = authority_host if port is None else f"{authority_host}:{port}"
    segments = parsed.path.split("/")
    for index, segment in enumerate(segments[:-1]):
        if unquote(segment).casefold() in _SENSITIVE_PATH_SEGMENTS:
            segments[index + 1] = "[REDACTED]"
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold().replace("-", "_")
        if not _SENSITIVE_QUERY_KEY.search(normalized_key):
            query.append((key, _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", item)))
    safe_url = urlunsplit((scheme, netloc, "/".join(segments), urlencode(query), ""))
    if len(safe_url) > 2000:
        raise ValueError(f"{field} exceeds the safe URL limit")
    return safe_url


def _hops(value) -> list:
    if not isinstance(value, (list, tuple)):
        return [{"state": "UNKNOWN"}] if value is not None else []
    result = []
    for item in value[:20]:
        try:
            safe = _url(item, "Observed hop") if isinstance(item, str) else None
        except ValueError:
            safe = None
        result.append(safe or {"state": "UNKNOWN"})
    return result


def _sanitize_extraction_capture(value) -> dict:
    if not isinstance(value, Mapping) or set(value) != {
            "schema_version", "raw_fields", "field_provenance", "raw_capture_ref"}:
        raise ValueError("Extraction capture must use the supported bounded schema")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Extraction capture schema version is unsupported")
    raw_fields = value.get("raw_fields")
    provenance = value.get("field_provenance")
    if (not isinstance(raw_fields, Mapping) or set(raw_fields) != set(_EXTRACTION_FIELDS)
            or not isinstance(provenance, Mapping) or set(provenance) != set(_EXTRACTION_FIELDS)):
        raise ValueError("Extraction capture must explicitly represent every supported field")
    raw_capture_ref = value.get("raw_capture_ref")
    if raw_capture_ref is not None and (
            not isinstance(raw_capture_ref, str) or raw_capture_ref != raw_capture_ref.strip()
            or not _OPAQUE_CAPTURE_REF.fullmatch(raw_capture_ref)
            or _SENSITIVE_CAPTURE_REF.search(raw_capture_ref)):
        raise ValueError("Extraction capture reference must be a bounded opaque identifier")

    sanitized_fields = {}
    sanitized_provenance = {}
    for field in _EXTRACTION_FIELDS:
        raw_value = raw_fields[field]
        field_source = provenance[field]
        if not isinstance(field_source, Mapping) or set(field_source) != {
                "source", "confidence", "review_required", "user_corrected"}:
            raise ValueError(f"Extraction provenance for {field} is invalid")
        source = field_source.get("source")
        confidence = field_source.get("confidence")
        review_required = field_source.get("review_required")
        user_corrected = field_source.get("user_corrected")
        if (not isinstance(source, str) or source not in _EXTRACTION_SOURCES
                or type(confidence) not in {int, float} or confidence < 0 or confidence > 1
                or not math.isfinite(confidence)
                or type(review_required) is not bool or type(user_corrected) is not bool):
            raise ValueError(f"Extraction provenance for {field} is invalid")
        if (field in _LOW_CONFIDENCE_IDENTITY_FIELDS and source != "UNKNOWN"
                and confidence < 0.8 and not review_required):
            raise ValueError(f"Extraction provenance for low-confidence identity field {field} requires review")
        if source == "UNKNOWN":
            if raw_value is not None or confidence != 0 or review_required:
                raise ValueError(f"Unknown extraction for {field} must remain explicit")
            sanitized_fields[field] = None
        else:
            if confidence <= 0 or not isinstance(raw_value, str):
                raise ValueError(f"Extracted value for {field} is invalid")
            if field in {"listing_url", "application_url"}:
                sanitized_fields[field] = _url(raw_value, f"Extracted {field}")
            else:
                sanitized_fields[field] = _plain_capture_text(
                    raw_value, field, _EXTRACTION_FIELD_LIMITS[field])
            if not sanitized_fields[field]:
                raise ValueError(f"Extracted value for {field} is empty")
        sanitized_provenance[field] = {
            "source": source,
            "confidence": float(confidence),
            "review_required": review_required,
            "user_corrected": user_corrected,
        }

    sanitized = {
        "schema_version": 1,
        "raw_fields": sanitized_fields,
        "field_provenance": sanitized_provenance,
        "raw_capture_ref": raw_capture_ref,
    }
    if len(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 40_000:
        raise ValueError("Extraction capture exceeds the safe metadata limit")
    return sanitized


def _sanitize_source_details(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 20:
        raise ValueError("Source details must be a bounded object")
    sanitized = {}
    sensitive_key = re.compile(r"(?i)(raw.?html|page.?text|cookie|password|secret|token|form.?answer|instruction)")
    for key, item in value.items():
        if (not isinstance(key, str) or not key or len(key) > 64
                or sensitive_key.search(key) or _RAW_CAPTURE_INPUT_KEY.search(key)):
            raise ValueError("Source details contain an unsupported or sensitive field")
        if key == "extraction_capture":
            sanitized[key] = _sanitize_extraction_capture(item)
        elif key == "observed_hops":
            sanitized[key] = _hops(item)
        elif key == "discovery_origin":
            if item is not None:
                sanitized[key] = _plain_capture_text(item, key, 253)
        elif key in {"source_url", "reference_url", "listing_url", "application_url"}:
            sanitized[key] = _url(item, key) if item is not None else None
        elif item is None or type(item) in {bool, int}:
            sanitized[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            sanitized[key] = item
        elif isinstance(item, str):
            sanitized[key] = _plain_capture_text(item, key, 2000)
        else:
            raise ValueError(f"Source detail {key} must be a bounded scalar")
    if len(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 48_000:
        raise ValueError("Source details exceed the safe metadata limit")
    return sanitized


def _validate_user_corrections(data: dict, source_details: dict) -> None:
    extraction = source_details.get("extraction_capture")
    if extraction is None:
        return
    final_values = {
        "title": data["title"],
        "company": data["company"],
        "description": data["description"],
        "location": data.get("location"),
        "application_url": data.get("application_url"),
    }
    for field, final_value in final_values.items():
        extracted_value = extraction["raw_fields"][field]
        if extracted_value is None or final_value is None:
            continue
        if field == "application_url":
            confirmed = final_value
        else:
            confirmed = _plain_capture_text(
                final_value, f"Confirmed {field}", _EXTRACTION_FIELD_LIMITS[field])
        if (confirmed != extracted_value
                and not extraction["field_provenance"][field]["user_corrected"]):
            raise ValueError(f"Changed extracted value for {field} must be marked as user corrected")


def _reject_raw_capture_content(data: dict) -> None:
    for key in data:
        if (isinstance(key, str)
                and (key == "extraction_capture" or _RAW_CAPTURE_INPUT_KEY.search(key))):
            raise ValueError("Manual capture must not persist raw capture content")


def capture_manual_job(store, submitted: dict) -> dict:
    """Save a manual capture without performing any network access."""
    data = dict(submitted)
    _reject_raw_capture_content(data)
    request_id = data["request_id"].strip()
    title = data["title"].strip()
    company = data["company"].strip()
    description = data["description"]
    if not request_id or not title or not company or not description.strip():
        raise ValueError("Job title, company, description, and request identifier are required")
    reference_url = _url(data.get("reference_url"), "Job reference URL")
    application_url = _url(data.get("application_url"), "Application URL")
    if reference_url is None and application_url is None:
        raise ValueError("Enter a job reference URL or an application URL")
    data["reference_url"] = reference_url
    data["application_url"] = application_url
    if data.get("discovery_origin") is not None:
        data["discovery_origin"] = _plain_capture_text(
            data["discovery_origin"], "Discovery origin", 253)
    if data.get("observed_hops") is not None:
        data["observed_hops"] = _hops(data["observed_hops"])
    if "source_details" in data:
        data["source_details"] = _sanitize_source_details(data["source_details"])
    _validate_user_corrections(data, data.get("source_details") or {})
    route_url = application_url or reference_url
    source_url = reference_url or application_url
    payload_size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if payload_size > 100_000:
        raise ValueError("Manual job source data exceeds the 100 KiB limit")

    job_id = identity("manual-job", request_id)
    company_id = identity("manual-company", request_id)
    route_id = identity("manual-route", request_id)
    posting_id = identity("manual-posting", request_id)
    timestamp = now()
    raw = {"source": "MANUAL_USER_ENTRY", "captured_at": timestamp,
           "submitted_fields": data}
    skill_data = extract_skills(title + " " + description)
    description_skills = extract_skills(description)
    skill_requirements = parse_skill_requirements(description, description_skills["mentions"])
    profile_id = data.get("profile_id")
    if profile_id:
        store.profile(profile_id)

    created = True
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        # The stable company row serializes concurrent retries with this key.
        upsert(connection, m.companies,
               {"id": company_id, "name": company, "domain": ""})
        connection.execute(select(m.companies.c.id).where(
            m.companies.c.id == company_id).with_for_update()).one()
        previous = connection.execute(select(m.postings).where(
            m.postings.c.id == posting_id).with_for_update()).mappings().one_or_none()
        if previous is not None:
            if previous["raw"].get("submitted_fields") != data:
                raise ManualCaptureConflict("Manual capture request identifier was already used for different content")
            created = False
        else:
            attributes = {
                "source_type": "MANUAL_USER_ENTRY",
                "captured_at": timestamp,
                "employment_type": "UNKNOWN",
                "experience_level": "UNKNOWN",
                "salary_source": "UNKNOWN",
                "salary_period": "UNKNOWN",
                "remote_regions": [],
                "status": "UNKNOWN",
                "company_identity": "USER_ENTERED_UNVERIFIED",
                "skill_evidence": "description_mentions",
                "skill_ids": skill_data["ids"],
                "skill_mentions": skill_data["mentions"],
                "required_skills": [row["skill"] for row in skill_requirements
                                    if row["strength"] == "REQUIRED"],
                "preferred_skills": [row["skill"] for row in skill_requirements
                                     if row["strength"] == "PREFERRED"],
                "nice_to_have_skills": [row["skill"] for row in skill_requirements
                                        if row["strength"] == "NICE_TO_HAVE"],
                "certifications": parse_certifications(description),
            }
            upsert(connection, m.jobs, {
                "id": job_id,
                "company_id": company_id,
                "requisition": request_id,
                "title": title,
                "description": description,
                "skills": skill_data["labels"],
                "location": data.get("location") or "Unknown",
                "remote_type": data.get("remote_type") or "UNKNOWN",
                "attributes": attributes,
            })
            upsert(connection, m.routes, {
                "id": route_id,
                "job_id": job_id,
                "company_id": company_id,
                "provider": "manual",
                "tenant": "user-entry",
                "requisition": request_id,
                "url": route_url,
            })
            persist_posting(connection, {
                "id": posting_id,
                "job_id": job_id,
                "source": "manual",
                "source_job_id": request_id,
                "source_url": source_url,
                "application_url": route_url,
                "raw": raw,
                "fetched_at": timestamp,
            })
            store._audit(connection, job_id, "MANUAL_JOB_CAPTURED", {
                "source": "MANUAL_USER_ENTRY", "captured_at": timestamp,
            })
            from services.source_candidates.catalogue import record_capture
            source_details = data.get("source_details") or {}
            record_capture(
                connection, profile_id=profile_id, request_id=request_id,
                job_id=job_id, application_id=None,
                discovery_origin=(data.get("discovery_origin") or
                                  source_details.get("discovery_origin")),
                source_url=reference_url, application_url=application_url,
                observed_hops=data.get("observed_hops") or source_details.get("observed_hops"),
                capture_method="MANUAL_CAPTURE",
            )

    return {"job": store.job(job_id), "created": created}
