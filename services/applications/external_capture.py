"""Record applications completed outside MJobs using canonical local records."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.dedupe.routes import load_job_routes
from services.dedupe.signals import canonical_application_url
from services.discovery.manual_capture import ManualCaptureConflict, capture_manual_job
from services.tracking.applications import (
    read_application_tracking, record_external_application_report,
    reserve_external_application_capture,
)


class ExternalCaptureConflict(ValueError):
    """A request identifier or reconciliation target is ambiguous/conflicting."""


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _http_url(value: str | None) -> str | None:
    if value is None:
        return None
    if value != value.strip() or not value:
        raise ValueError("Application URL must be a complete HTTP or HTTPS URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Application URL must be a complete HTTP or HTTPS URL") from error
    if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Application URL must be a complete HTTP or HTTPS URL")
    return value


def _utc(value: str | datetime) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Application timestamp must include a timezone")
    normalized = parsed.astimezone(timezone.utc)
    precision = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=precision).replace("+00:00", "Z")


def _capture_id(profile_id: str, request_id: str) -> str:
    return identity("external-application-capture-event", profile_id, request_id)


def _existing_capture(store, profile_id: str, request_id: str):
    with store.engine.connect() as connection:
        return connection.execute(select(m.audit).where(
            m.audit.c.id == _capture_id(profile_id, request_id))).mappings().one_or_none()


def _job_routes(store):
    with store.engine.connect() as connection:
        rows = connection.execute(select(
            m.routes.c.id.label("route_id"), m.routes.c.job_id,
            m.routes.c.url.label("route_url"), m.jobs.c.title,
            m.companies.c.name.label("company"),
        ).select_from(m.routes.join(m.jobs, m.jobs.c.id == m.routes.c.job_id)
          .join(m.companies, m.companies.c.id == m.jobs.c.company_id))).mappings()
        return [dict(row) for row in rows]


def _resolve(store, data: dict):
    routes = _job_routes(store)
    requested = canonical_application_url(data["application_url"])
    if requested:
        matches = [row for row in routes
                   if canonical_application_url(row["route_url"]) == requested]
        job_ids = {row["job_id"] for row in matches}
        if len(job_ids) > 1:
            raise ExternalCaptureConflict("Application URL matches multiple canonical jobs")
        if matches:
            chosen = sorted(matches, key=lambda row: row["route_id"])[0]
            return chosen["job_id"], chosen["route_id"], "APPLICATION_URL"

    company, title = _normalize(data["company"]), _normalize(data["title"])
    matches = [row for row in routes
               if _normalize(row["company"]) == company
               and _normalize(row["title"]) == title]
    by_job = {}
    for row in matches:
        by_job.setdefault(row["job_id"], []).append(row)
    if len(by_job) > 1:
        raise ExternalCaptureConflict("Company and title match multiple canonical jobs")
    if by_job:
        job_id, rows = next(iter(by_job.items()))
        route = sorted(rows, key=lambda row: row["route_id"])[0]
        return job_id, route["route_id"], "COMPANY_TITLE_EXACT"
    return None


def _content_hash(data: dict) -> str:
    canonical = {
        "profile_id": data["profile_id"], "company": data["company"].strip(),
        "title": data["title"].strip(), "source": data["source"],
        "applied_at": data["applied_at"],
        "application_url": canonical_application_url(data["application_url"]),
        "job_description": data["job_description"].strip() if data["job_description"] else None,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_source_candidate(store, data, job_id, application_id):
    from services.source_candidates.catalogue import SourceCandidateConflict, record_capture
    try:
        with store.engine.begin() as connection:
            return record_capture(
                connection, profile_id=data["profile_id"], request_id=data["request_id"],
                job_id=job_id, application_id=application_id,
                discovery_origin=data.get("discovery_origin"),
                source_url=data.get("source_url"),
                application_url=data.get("application_url"),
                observed_hops=data.get("observed_hops"),
                capture_method="EXTERNAL_APPLICATION",
            )
    except SourceCandidateConflict as error:
        raise ExternalCaptureConflict(str(error)) from error


def _response(store, profile_id: str, application_id: str, job_id: str,
              reconciliation: str, created: bool):
    return {
        "created": created,
        "application_id": application_id,
        "job_id": job_id,
        "reconciliation": reconciliation,
        "application": store.application(application_id),
        "tracking": read_application_tracking(store, profile_id, application_id),
    }


def capture_external_application(store, submitted: dict) -> dict:
    """Create/reuse a profile-scoped application without contacting an employer."""
    data = dict(submitted)
    data["company"] = data["company"].strip()
    data["title"] = data["title"].strip()
    data["source"] = data["source"].upper()
    data["application_url"] = _http_url(data.get("application_url"))
    data["source_url"] = _http_url(data.get("source_url"))
    data["observed_hops"] = data.get("observed_hops") or []
    if (type(data["observed_hops"]) is not list or len(data["observed_hops"]) > 20
            or any(type(hop) is not str or _http_url(hop) is None for hop in data["observed_hops"])):
        raise ValueError("Observed application hops must be valid HTTP or HTTPS URLs")
    if data.get("discovery_origin") is not None:
        if type(data["discovery_origin"]) is not str:
            raise ValueError("Discovery origin must be text")
        data["discovery_origin"] = " ".join(data["discovery_origin"].split()) or None
    data["applied_at"] = _utc(data["applied_at"])
    data["job_description"] = data.get("job_description")
    if not data["company"] or not data["title"] or not data["request_id"].strip():
        raise ValueError("Company, title, and request identifier are required")
    if data["source"] not in {"MANUAL", "REFERRAL", "BROWSER", "OTHER"}:
        raise ValueError("Unsupported external application source")
    store.profile(data["profile_id"])
    fingerprint = _content_hash(data)

    prior = _existing_capture(store, data["profile_id"], data["request_id"])
    if prior is not None:
        payload = prior["payload"]
        if (prior["action"] != "EXTERNAL_APPLICATION_CAPTURE"
                or payload.get("content_sha256") != fingerprint):
            raise ExternalCaptureConflict(
                "External application request identifier was already used for different content")
        _record_source_candidate(store, data, payload["job_id"], payload["application_id"])
        return _response(store, data["profile_id"], payload["application_id"],
                         payload["job_id"], payload["reconciliation"], False)

    resolved = _resolve(store, data)
    if resolved is None and not data["application_url"]:
        raise ValueError("A new application requires an application URL for safe reconciliation")
    try:
        reserve_external_application_capture(
            store, data["profile_id"], data["request_id"], fingerprint)
    except ValueError as error:
        raise ExternalCaptureConflict(str(error)) from error
    if resolved is None:
        description = (data["job_description"] or
                       "A job description was not provided when this external application was recorded.")
        job_request_id = identity("external-application-job", data["profile_id"],
                                  data["request_id"])
        try:
            captured = capture_manual_job(store, {
                "request_id": job_request_id,
                "title": data["title"], "company": data["company"],
                "description": description,
                "location": "Unknown", "remote_type": "UNKNOWN",
                "application_url": data["application_url"],
                "source_details": {"capture_kind": "EXTERNAL_APPLICATION",
                                   "source": data["source"]},
            })
        except ManualCaptureConflict as error:
            raise ExternalCaptureConflict(str(error)) from error
        job_id = captured["job"]["id"]
        route_id = load_job_routes(store, job_id)[0]["id"]
        reconciliation = "CREATED_MINIMAL_JOB"
    else:
        job_id, route_id, reconciliation = resolved

    application_id = identity(job_id, data["profile_id"])
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current_profile = connection.execute(select(m.profiles).where(
            m.profiles.c.id == data["profile_id"]).with_for_update()).mappings().one()
        application = connection.execute(select(m.applications).where(
            m.applications.c.job_id == job_id,
            m.applications.c.profile_id == data["profile_id"],
        ).with_for_update()).mappings().one_or_none()
        if application is None:
            connection.execute(m.applications.insert().values(
                id=application_id, job_id=job_id, route_id=route_id,
                profile_id=data["profile_id"], profile_version=current_profile["version"],
                state="EXTERNALLY_APPLIED", target="EXTERNAL_REPORTED",
                fact_ids=[], trace=["EXTERNAL_APPLICATION_REPORTED"],
                document_version=1, confirmation=None, created_at=now(),
                submitted_at=data["applied_at"],
            ))
    try:
        report = record_external_application_report(
            store, data["profile_id"], application_id,
            "external:" + data["request_id"], data["request_id"], fingerprint,
            data["source"], data["applied_at"], data["application_url"],
            job_id, reconciliation,
        )
    except ValueError as error:
        raise ExternalCaptureConflict(str(error)) from error
    _record_source_candidate(store, data, job_id, application_id)
    return {"created": report["created"], "application_id": application_id, "job_id": job_id,
            "reconciliation": reconciliation, "application": store.application(application_id),
            "tracking": report["tracking"]}
