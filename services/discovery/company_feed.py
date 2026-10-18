"""Normalize and persist discovered employer feed rows through canonical jobs."""
from __future__ import annotations

import json

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.company_discovery.sources import _token, _url
from services.discovery.search_semantics import (
    classify_location, normalize_employment_type, normalize_experience_level,
    normalize_title, parse_education_requirement, parse_salary, normalize_reported_timestamp,
)
from services.discovery.skill_taxonomy import extract_skills
from services.discovery.requirement_parsers import parse_job_requirements
from services.discovery.negative_filters import parse_negative_conditions
from services.discovery.sources.collect import persist_posting
from services.discovery.sources.greenhouse import plain_text
from services.company_discovery.quality import posting_metrics, record_observation
from services.discovery.feed_items import _application_url, describe_feed_item
from services.company_discovery.link_evidence import validate_reports, record_link_evidence
from services.dedupe.source_values import lock_candidate_job, record_candidate, resolve_job_values
from services.discovery.lifecycle import status_claim
from services.dedupe.ingestion_identity import canonical_ingestion_job, lock_company_ingestion
from services.audit_history import record_job_merged


def _validate_source_identity(store, *, source_id, company_id, provider, tenant, endpoint, strategy):
    with store.engine.connect() as connection:
        source = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id
        )).mappings().one_or_none()
    try:
        caller_company = _token(company_id, "COMPANY_ID", 128)
        caller_provider = _token(provider, "PROVIDER", 32, fold=True)
        caller_tenant = _token(tenant, "TENANT", 128)
        caller_endpoint = _url(endpoint, "ENDPOINT", required=True)
        caller_strategy = _token(strategy, "COLLECTION_STRATEGY", 32, fold=True).upper()
    except ValueError:
        raise ValueError("COMPANY_FEED_SOURCE_MISMATCH")
    if (source is None or source["company_id"] != caller_company or source["provider"] != caller_provider
            or source["tenant"] != caller_tenant or source["endpoint"] != caller_endpoint
            or source["collection_strategy"] != caller_strategy):
        raise ValueError("COMPANY_FEED_SOURCE_MISMATCH")
    return source


def import_company_feed_jobs(store, *, company_id, source_id, provider, tenant,
                             endpoint, strategy, items, profile_id=None, profile_version=None,
                             check=None, fetch_available=None, link_reports=None,
                             import_guard=None, import_receipt=None, extra_profile_ids=()):
    """Persist bounded public feed rows and optional profile matches idempotently."""
    if fetch_available is not None and type(fetch_available) is not bool:
        raise ValueError("COMPANY_FEED_FETCH_AVAILABILITY_INVALID")
    check = check or (lambda: None)
    source = _validate_source_identity(store, source_id=source_id, company_id=company_id,
                                       provider=provider, tenant=tenant, endpoint=endpoint,
                                       strategy=strategy)
    # Keep the caller's endpoint for urljoin: its trailing slash carries relative
    # path semantics even when canonical identity comparison removes it.
    company_id, provider, tenant = source["company_id"], source["provider"], source["tenant"]
    strategy = source["collection_strategy"]
    items = list(items or [])
    profile = store.profile(profile_id, profile_version) if profile_id else None
    extra_profiles = [store.profile(identifier) for identifier in sorted(set(extra_profile_ids)) if identifier != profile_id]
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
    pending_merges = []
    pending_source_candidates = []
    batch_postings = {}
    batch_identities = {}
    guard_rejected = False
    try:
        reports = validate_reports(items, strategy, endpoint, link_reports, timestamp)
        with store.engine.begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            lock_company_ingestion(connection, [company_id])
            if import_guard:
                try:
                    import_guard(connection)
                except Exception:
                    # A revoked local authorization is not a failed employer feed.
                    guard_rejected = True
                    raise
            current_source = connection.execute(select(m.company_sources).where(
                m.company_sources.c.id == source_id).with_for_update()).mappings().one()
            if any(current_source[key] != source[key] for key in (
                    'company_id', 'provider', 'tenant', 'endpoint', 'collection_strategy')):
                raise ValueError('COMPANY_FEED_SOURCE_CHANGED')
            check()
            # Lock jobs in stable source-key order across overlapping feed refreshes.
            for item in sorted(items, key=lambda row: str(row.get('id') or row.get('guid') or
                    _application_url(row, strategy, endpoint) or '') if isinstance(row, dict) else ''):
                check()
                entry = describe_feed_item(item, strategy, endpoint)
                if entry is None:
                    continue
                title, application_url = entry.title, entry.application_url
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
                published = item.get("date_published") or item.get("published") or item.get("pubdate")
                external_key = entry.external_key
                requisition = external_key if len(external_key) <= 128 else identity(external_key)
                native_job_id = identity(company_id, provider, tenant, requisition)
                posting_source = 'company-feed:' + str(source_id)
                source_job_id = identity(posting_source, external_key)
                posting_id = identity(posting_source, source_job_id)
                posting_already_persisted = connection.execute(select(m.postings.c.id).where(
                    m.postings.c.id == posting_id)).first() is not None
                canonical = canonical_ingestion_job(connection, company_id=company_id,
                    proposed_id=native_job_id, posting_id=posting_id, application_url=application_url,
                    provider=provider, tenant=tenant, requisition=requisition, batch_identities=batch_identities)
                job_id = canonical['id'] if canonical else native_job_id
                batch_identities.setdefault(job_id, []).append((provider, tenant, requisition))
                previous = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one_or_none()
                previous = dict(previous or {})
                first_values = connection.execute(select(m.posting_value_candidates.c.normalized).where(
                    m.posting_value_candidates.c.posting_id == posting_id,
                    m.posting_value_candidates.c.normalized['attributes']['reported_posted_at']
                        .as_string().is_not(None)).order_by(
                        m.posting_value_candidates.c.observed_at, m.posting_value_candidates.c.id)
                    .limit(1)).scalar_one_or_none()
                previous_dates = (first_values or {}).get('attributes') or (
                    previous if previous.get('source_id') == source_id else {})
                attributes = {
                "lifecycle_claim": status_claim(item.get('status')),
                "provider": provider, "tenant": tenant, "source_id": source_id,
                "source_endpoint": endpoint, "collection_strategy": strategy,
                    **normalize_reported_timestamp(published,
                        reference_at=timestamp, previous=previous_dates),
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
                lock_candidate_job(connection, {**job,
                    'requisition': canonical['requisition'] if canonical else requisition})
                if job_id == native_job_id:
                    upsert(connection, m.routes, {"id": job_id, "job_id": job_id, "company_id": company_id,
                        "provider": provider, "tenant": tenant, "requisition": requisition,
                        "url": application_url})
                raw_key = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if posting_id in batch_postings and batch_postings[posting_id] != raw_key:
                    raise ValueError('SOURCE_DUPLICATE_JOB_ID')
                batch_postings[posting_id] = raw_key
                persist_posting(connection, {"id": posting_id, "job_id": job_id,
                    "source": posting_source, "source_job_id": source_job_id, "source_url": endpoint,
                    "application_url": application_url, "raw": item, "fetched_at": timestamp})
                if not posting_already_persisted and canonical is not None and job_id != native_job_id:
                    pending_merges.append((posting_id, job_id))
                pending_candidates.append((posting_id, job_id, external_key, {key: value for key, value in job.items()
                                                                if key not in {'id', 'company_id'}}))
                pending_source_candidates.append((posting_id, job_id, application_url))
                completeness, has_salary = posting_metrics(job, application_url)
                completeness_sum += completeness
                salary_known += int(has_salary)
                imported += 1
            observation_at = now()
            observation_id = record_observation(
                connection, company_source_id=source_id, observed_at=observation_at,
                source_endpoint=endpoint,
                succeeded=True, available=fetch_available, enumerated=len(items), accepted=imported,
                completeness_sum=completeness_sum, salary_known=salary_known,
                links_checked=sum(r.result != 'UNKNOWN' for r in reports.values()) if link_reports is not None else None,
                links_accurate=sum(r.result == 'ACCURATE' for r in reports.values()) if link_reports is not None else None,
            )
            linked = set()
            for posting_id, _, external_key, values in pending_candidates:
                candidate_id = record_candidate(connection, posting_id=posting_id, company_source_id=source_id,
                                 observation_id=observation_id, normalized=values)
                if external_key in reports and candidate_id not in linked:
                    record_link_evidence(connection, candidate_id, reports[external_key])
                    linked.add(candidate_id)
            for job_id in sorted({entry[1] for entry in pending_candidates}):
                resolve_job_values(connection, job_id, at=observation_at, seen_at=timestamp, profile=profile)
                for extra_profile in extra_profiles:
                    resolve_job_values(connection, job_id, at=observation_at, seen_at=timestamp, profile=extra_profile)
            imported_job_ids = {entry[1] for entry in pending_candidates}
            current_profiles = ([profile] if profile else []) + extra_profiles
            evaluated_profiles = [current_profile for current_profile in current_profiles
                if connection.execute(select(m.profiles.c.version).where(
                    m.profiles.c.id == current_profile['id'])).scalar_one_or_none() == current_profile['version']]
            from services.source_candidates.catalogue import record_supported_collection
            for current_profile in evaluated_profiles:
                for posting_id, job_id, application_url in pending_source_candidates:
                    record_supported_collection(
                        connection, profile_id=current_profile['id'],
                        request_id=identity("supported-feed-source-capture", source_id,
                                            posting_id, application_url),
                        job_id=job_id, source_url=endpoint,
                        application_url=application_url,
                    )
            for posting_id, merged_job_id in pending_merges:
                for current_profile in evaluated_profiles:
                    record_job_merged(connection, current_profile["id"], merged_job_id, posting_id)
            matched_job_ids = set()
            for current_profile in evaluated_profiles:
                matched_job_ids.update(connection.execute(select(m.matches.c.job_id).where(
                    m.matches.c.job_id.in_(imported_job_ids),
                    m.matches.c.profile_id == current_profile['id'],
                    m.matches.c.profile_version == current_profile['version'],
                    m.matches.c.score > 0)).scalars())
            audit_details = {
                "company_source_id": source_id, "provider": provider, "strategy": strategy,
                "jobs_enumerated": len(items), "jobs_imported": imported, "profile_id": profile_id,
                "evaluated_profile_count": len({(item['id'], item['version']) for item in evaluated_profiles})}
            if evaluated_profiles:
                audit_details["matched_jobs"] = len(matched_job_ids)
            store._audit(connection, company_id, "COMPANY_FEED_IMPORT_COMPLETE", audit_details)
            # The locked source and its successful collection commit together.
            # Compare parsed instants so an older in-flight refresh cannot rewind it.
            from datetime import datetime
            previous_success = current_source['last_success']
            if not previous_success or datetime.fromisoformat(previous_success.replace('Z', '+00:00')) < datetime.fromisoformat(observation_at):
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == source_id)
                                   .values(last_success=observation_at))
            if import_receipt:
                import_receipt(connection)
    except Exception as failure:
        from workers.operations import OperationCancelled
        if guard_rejected or isinstance(failure, OperationCancelled) or import_guard and str(failure).startswith('MONITOR_'):
            raise
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
