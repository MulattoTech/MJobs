"""Normalize and persist discovered employer feed rows through canonical jobs."""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit
import json

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.company_discovery.sources import _token, _url
from services.discovery.search_semantics import (
    classify_location, normalize_employment_type, normalize_experience_level,
    normalize_title, parse_education_requirement, parse_salary, parse_timestamp,
)
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.negative_filters import parse_negative_conditions
from services.discovery.sources.collect import persist_posting
from services.discovery.sources.greenhouse import plain_text
from services.company_discovery.quality import posting_metrics, record_observation
from services.dedupe.source_values import lock_candidate_job, record_candidate, resolve_job_values


def _application_url(item, strategy, endpoint):
    value = item.get("url") or item.get("external_url") if strategy == "PUBLIC_JSON" else item.get("link")
    value = str(value or "").strip()
    if not value:
        return None
    target = urljoin(endpoint, value)
    try:
        parsed = urlsplit(target)
        if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        parsed.port
    except ValueError:
        return None
    return target[:2000] if len(target) <= 2000 else None


def _validate_source_identity(store, *, source_id, company_id, provider, tenant, endpoint):
    with store.engine.connect() as connection:
        source = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id
        )).mappings().one_or_none()
    try:
        caller_company = _token(company_id, "COMPANY_ID", 128)
        caller_provider = _token(provider, "PROVIDER", 32, fold=True)
        caller_tenant = _token(tenant, "TENANT", 128)
        caller_endpoint = _url(endpoint, "ENDPOINT", required=True)
    except ValueError:
        raise ValueError("COMPANY_FEED_SOURCE_MISMATCH")
    if (source is None or source["company_id"] != caller_company or source["provider"] != caller_provider
            or source["tenant"] != caller_tenant or source["endpoint"] != caller_endpoint):
        raise ValueError("COMPANY_FEED_SOURCE_MISMATCH")
    return source


def import_company_feed_jobs(store, *, company_id, source_id, provider, tenant,
                             endpoint, strategy, items, profile_id=None, profile_version=None,
                             check=None, fetch_available=None):
    """Persist bounded public feed rows and optional profile matches idempotently."""
    if fetch_available is not None and type(fetch_available) is not bool:
        raise ValueError("COMPANY_FEED_FETCH_AVAILABILITY_INVALID")
    check = check or (lambda: None)
    source = _validate_source_identity(store, source_id=source_id, company_id=company_id,
                                       provider=provider, tenant=tenant, endpoint=endpoint)
    # Keep the caller's endpoint for urljoin: its trailing slash carries relative
    # path semantics even when canonical identity comparison removes it.
    company_id, provider, tenant = source["company_id"], source["provider"], source["tenant"]
    items = list(items or [])
    profile = store.profile(profile_id, profile_version) if profile_id else None
    usable = set()
    if profile:
        usable = {fact["text"].strip().casefold() for fact in profile["facts"]
                  if fact["kind"] == "skill" and fact["verified"] and fact["user_approved"]
                  and fact["source"].strip() and fact["confidence"] >= 80}
    timestamp = now()
    imported = 0
    completeness_sum = 0.0
    salary_known = 0
    pending_candidates = []
    batch_postings = {}
    try:
        with store.engine.begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            current_source = connection.execute(select(m.company_sources).where(
                m.company_sources.c.id == source_id).with_for_update()).mappings().one()
            if any(current_source[key] != source[key] for key in ('company_id', 'provider', 'tenant', 'endpoint')):
                raise ValueError('COMPANY_FEED_SOURCE_CHANGED')
            check()
            # Lock jobs in stable source-key order across overlapping feed refreshes.
            for item in sorted(items, key=lambda row: str(row.get('id') or row.get('guid') or
                    _application_url(row, strategy, endpoint) or '') if isinstance(row, dict) else ''):
                check()
                if not isinstance(item, dict):
                    continue
                title = plain_text(item.get("title"))[:500]
                application_url = _application_url(item, strategy, endpoint)
                if not title or application_url is None:
                    continue
                description_value = (item.get("content_text") or item.get("content_html") or item.get("description")
                                     or item.get("summary") or "")
                description = plain_text(description_value)[:50_000]
                location = plain_text(item.get("location") or "Unknown")[:1000] or "Unknown"
                location_facts = classify_location(location, description)
                skill_data = extract_skills(title + " " + description)
                title_info = normalize_title(title)
                experience = normalize_experience_level(title)
                education = parse_education_requirement(description)
                requirements = parse_job_requirements(description)
                negative = parse_negative_conditions(description, requirements["clearance"])
                salary = parse_salary(description, "EMPLOYER_PROVIDED")
                published = parse_timestamp(item.get("date_published") or item.get("published") or item.get("pubdate"))
                external_key = str(item.get("id") or item.get("guid") or application_url).strip()
                requisition = external_key if len(external_key) <= 128 else identity(external_key)
                job_id = identity(company_id, provider, tenant, requisition)
                previous = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one_or_none()
                previous = dict(previous or {})
                attributes = {
                "provider": provider, "tenant": tenant, "source_id": source_id,
                "source_endpoint": endpoint, "collection_strategy": strategy,
                    "reported_posted_at": published.isoformat() if published else None,
                "first_seen_at": previous.get("first_seen_at") or timestamp,
                "last_seen_at": timestamp, "last_collected_at": timestamp,
                "employment_type": normalize_employment_type(title + " " + description),
                "experience_level": experience["level"], "experience_level_raw": experience["raw"],
                "normalized_title": title_info["normalized_title"], "title_family": title_info["title_family"],
                "skill_ids": skill_data["ids"], "skill_mentions": skill_data["mentions"],
                "skill_evidence": "description_mentions", "education_requirement": education["classification"],
                "education_requirement_raw": education["raw"],
                "experience_requirements": requirements["experience"],
                "certifications": requirements["certifications"], "clearance": requirements["clearance"],
                "work_authorization": requirements["work_authorization"], "negative_conditions": negative,
                "salary_min": salary["salary_min"], "salary_max": salary["salary_max"],
                "salary_period": salary["salary_period"], "salary_currency": salary["currency"],
                "salary_source": salary["salary_source"], "salary_raw": salary["salary_raw"],
                "remote_regions": location_facts["remote_regions"],
                "remote_indicated": location_facts["remote_indicated"],
                "country": location_facts["country"], "state": location_facts["state"],
                "city": location_facts["city"], "application_url_status": "PROVIDER_SUPPLIED",
                "submission_capability": "MANUAL_ONLY", "fictional": False,
                }
                job = {"id": job_id, "company_id": company_id, "requisition": requisition,
                       "title": title, "description": description, "skills": skill_data["labels"],
                       "location": location, "remote_type": location_facts["remote_type"],
                       "attributes": attributes}
                lock_candidate_job(connection, job)
                upsert(connection, m.routes, {"id": job_id, "job_id": job_id, "company_id": company_id,
                    "provider": provider, "tenant": tenant, "requisition": requisition,
                    "url": application_url})
                source = "company-feed:" + str(source_id)
                source_job_id = identity(source, external_key)
                posting_id = identity(source, source_job_id)
                raw_key = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if posting_id in batch_postings and batch_postings[posting_id] != raw_key:
                    raise ValueError('SOURCE_DUPLICATE_JOB_ID')
                batch_postings[posting_id] = raw_key
                persist_posting(connection, {"id": posting_id, "job_id": job_id,
                    "source": source, "source_job_id": source_job_id, "source_url": endpoint,
                    "application_url": application_url, "raw": item, "fetched_at": timestamp})
                pending_candidates.append((posting_id, job_id, {key: value for key, value in job.items()
                                                                if key not in {'id', 'company_id'}}))
                completeness, has_salary = posting_metrics(job, application_url)
                completeness_sum += completeness
                salary_known += int(has_salary)
                imported += 1
            store._audit(connection, company_id, "COMPANY_FEED_IMPORT_COMPLETE", {
                "company_source_id": source_id, "provider": provider, "strategy": strategy,
                "jobs_enumerated": len(items), "jobs_imported": imported, "profile_id": profile_id,
            })
            observation_at = now()
            observation_id = record_observation(
                connection, company_source_id=source_id, observed_at=observation_at,
                source_endpoint=endpoint,
                succeeded=True, available=fetch_available, enumerated=len(items), accepted=imported,
                completeness_sum=completeness_sum, salary_known=salary_known,
                links_checked=None, links_accurate=None,
            )
            for posting_id, _, values in pending_candidates:
                record_candidate(connection, posting_id=posting_id, company_source_id=source_id,
                                 observation_id=observation_id, normalized=values)
            for job_id in sorted({entry[1] for entry in pending_candidates}):
                resolve_job_values(connection, job_id, at=observation_at, seen_at=timestamp, profile=profile)
    except Exception as failure:
        with store.engine.begin() as connection:
            record_observation(
                connection, company_source_id=source_id, observed_at=now(),
                source_endpoint=endpoint,
                succeeded=False, available=fetch_available, enumerated=len(items), accepted=None,
                completeness_sum=None, salary_known=None, links_checked=None,
                links_accurate=None, error_code=type(failure).__name__,
            )
        raise
    return imported
