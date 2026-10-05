"""Persist user-entered jobs through the canonical job, route, and posting model."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.discovery.sources.collect import persist_posting
from services.discovery.skill_taxonomy import extract_skills


class ManualCaptureConflict(ValueError):
    """An idempotency key was reused for different user-entered content."""


def _url(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a complete HTTP or HTTPS URL")
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError as error:
        raise ValueError(f"{field} must be a complete HTTP or HTTPS URL") from error
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or not valid_port):
        raise ValueError(f"{field} must be a complete HTTP or HTTPS URL")
    return value


def capture_manual_job(store, submitted: dict) -> dict:
    """Save a manual capture without performing any network access."""
    data = dict(submitted)
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

    return {"job": store.job(job_id), "created": created}
