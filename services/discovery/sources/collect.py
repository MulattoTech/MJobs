"""Durable per-board policy and provider-independent persistence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import httpx

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.company_discovery.sources import register_ats_tenant, upsert_company_job_source
from services.company_discovery.quality import posting_metrics, record_observation
from services.dedupe.source_values import lock_candidate_job, record_candidate, resolve_job_values
from services.discovery.sources.contract import SourceAdapter, SourceCapability
from services.discovery.sources.greenhouse import SourceError, tenant_identifier
from services.discovery.sources.registry import source_adapter


def configure_source(store, tenant, enabled=True, poll_interval=300, concurrency=1,
                     request_timeout=10, rate_limit=60, jitter_seconds=30):
    tenant = tenant_identifier(tenant)
    values = (poll_interval, concurrency, request_timeout, rate_limit, jitter_seconds)
    if type(enabled) is not bool or any(type(value) is not int for value in values):
        raise ValueError("Source policy values must use explicit boolean/integer types")
    if not 300 <= poll_interval <= 604800 or not 1 <= concurrency <= 8 or not 2 <= request_timeout <= 60:
        raise ValueError("Source poll interval, concurrency, or request timeout is outside supported bounds")
    if not 1 <= rate_limit <= 600 or not 0 <= jitter_seconds <= 300:
        raise ValueError("Source rate limit or jitter is outside supported bounds")
    source_id = identity("greenhouse", tenant)
    policy = {"provider": "greenhouse", "tenant": tenant, "request_timeout": request_timeout,
              "rate_limit": rate_limit, "concurrency": concurrency}
    capabilities = source_adapter(policy).capabilities()
    with store.engine.begin() as connection:
        upsert(connection, m.job_sources, {"id": source_id, "provider": "greenhouse", "tenant": tenant,
            "name": tenant, "enabled": enabled, "strategy": capabilities.strategy.value,
            "capabilities": capabilities.as_dict(), "poll_interval": poll_interval, "concurrency": concurrency,
            "request_timeout": request_timeout, "rate_limit": rate_limit, "jitter_seconds": jitter_seconds,
            "health_status": "NOT_CHECKED"})
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
            enabled=enabled, strategy=capabilities.strategy.value, capabilities=capabilities.as_dict(),
            poll_interval=poll_interval, concurrency=concurrency, request_timeout=request_timeout,
            rate_limit=rate_limit, jitter_seconds=jitter_seconds))
        store._audit(connection, source_id, "SOURCE_CONFIGURED", {
            "provider": "greenhouse", "tenant": tenant, "strategy": capabilities.strategy.value,
            "poll_interval": poll_interval, "concurrency": concurrency,
            "request_timeout": request_timeout, "rate_limit": rate_limit, "jitter_seconds": jitter_seconds})
    return source_id


def persist_posting(connection, data):
    # Reserve the ID before comparing: a SELECT-before-upsert race could reparent
    # a posting when concurrent PostgreSQL collectors both initially see no row.
    insert = {'postgresql': postgres_insert, 'sqlite': sqlite_insert}[connection.dialect.name]
    # Both the ID and (source, native ID) identify a posting. An explicit conflict
    # target can still lose a concurrent race on the other unique constraint.
    connection.execute(insert(m.postings).values(**data).on_conflict_do_nothing())
    previous = connection.execute(select(m.postings).where(m.postings.c.id == data['id']).with_for_update()).mappings().one_or_none()
    if previous is None or any(previous[key] != data[key] for key in ('job_id', 'source', 'source_job_id')):
        raise ValueError('POSTING_SOURCE_IDENTITY_CONFLICT')
    connection.execute(update(m.postings).where(m.postings.c.id == data['id']).values(
        **{key: data[key] for key in ('source_url', 'application_url', 'raw', 'fetched_at')}))
    checksum = hashlib.sha256(json.dumps(data["raw"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    upsert(connection, m.posting_snapshots, {"id": identity(data["id"], checksum), "posting_id": data["id"],
        "raw": data["raw"], "sha256": checksum, "retrieved_at": data["fetched_at"]})


def collect_source(store, source_id, profile_id, profile_version=None, check=None, transport=None):
    check = check or (lambda: None)
    profile = store.profile(profile_id, profile_version)
    check()
    timestamp = now()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        source = dict(connection.execute(select(m.job_sources).where(m.job_sources.c.id == source_id).with_for_update()).mappings().one())
        if not source["enabled"]:
            raise SourceError("SOURCE_DISABLED")
        if source["backoff_until"] and source["backoff_until"] > timestamp:
            raise SourceError("SOURCE_BACKOFF")
        if source["last_attempt"] and datetime.fromisoformat(source["last_attempt"]) + timedelta(seconds=source["poll_interval"]) > datetime.now(timezone.utc):
            raise SourceError("SOURCE_COOLDOWN")
        adapter: SourceAdapter = source_adapter(source, transport=transport, check=check)
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(last_attempt=timestamp, health_status="COLLECTING"))
    available = None
    observed_endpoint = None
    enumerated = accepted = completeness_sum = salary_known = None
    try:
        batch = adapter.search()
        observed_endpoint = batch.endpoint
        available = True
        enumerated = len(batch.items)
        normalized = []
        rejected = 0
        for item in batch.items:
            check()
            try:
                normalized.append((item, adapter.normalize(item.raw)))
            except SourceError:
                rejected += 1
        accepted = len(normalized)
        metrics = [posting_metrics(job, job.get('application_url')) for _, job in normalized]
        completeness_sum = sum(metric[0] for metric in metrics)
        salary_known = sum(int(metric[1]) for metric in metrics)
        if batch.items and not normalized:
            raise SourceError("SOURCE_NO_VALID_JOBS")
        capabilities = adapter.capabilities()
        strategy = capabilities.strategy.value
        direct_application = capabilities.supports(SourceCapability.DIRECT_APPLICATION)
        usable = {fact["text"].strip().casefold() for fact in profile["facts"] if fact["kind"] == "skill"
                  and fact["verified"] and fact["user_approved"] and fact["source"].strip() and fact["confidence"] >= 80}
        with store.engine.begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            check()
            companies = {}
            company_sources = {}
            pending_candidates = []
            batch_postings = {}
            for company in ([batch.source_company] if batch.source_company else []) + [item.company for item, _ in normalized]:
                company_id = identity(*company.identity_parts)
                companies[company_id] = company
            for company_id, company in sorted(companies.items()):
                upsert(connection, m.companies, {"id": company_id, "name": company.name, "domain": company.domain}, replace=True)
                tenant_record = register_ats_tenant(
                    connection, company_id, provider=source["provider"], tenant_identifier=source["tenant"],
                    confidence=0.9, evidence=["configured public job source adapter"],
                )
                company_source = upsert_company_job_source(
                    connection, company_id=company_id, provider=source["provider"], tenant_identifier=source["tenant"],
                    public_api_url=batch.endpoint if strategy in {"API", "PUBLIC_JSON"} else None,
                    feed_url=batch.endpoint if strategy == "FEED" else None,
                    search_url=batch.endpoint if strategy in {"PUBLIC_HTML", "BROWSER"} else None,
                    collection_strategy=strategy, confidence=0.9, enabled=True, priority="NORMAL",
                    discovered_automatically=False, ats_tenant_id=tenant_record["id"],
                    last_validation=timestamp, last_success=timestamp,
                )
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == company_source['id'])
                                   .values(collector_source_id=source_id))
                company_sources[company_id] = company_source['id']
            for item, job in sorted(normalized, key=lambda pair: (identity(*pair[0].company.identity_parts), pair[1]['requisition'])):
                company_id = identity(*item.company.identity_parts)
                job_id = identity(company_id, source["provider"], source["tenant"], job["requisition"])
                url = job.pop("application_url")
                existing_attributes = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one_or_none()
                existing_attributes = dict(existing_attributes or {})
                job["attributes"].update(
                    provider=source['provider'], tenant=source['tenant'],
                    source_id=source_id,
                    source_endpoint=batch.endpoint,
                    first_seen_at=existing_attributes.get("first_seen_at") or timestamp,
                    last_seen_at=timestamp,
                    last_collected_at=timestamp,
                )
                lock_candidate_job(connection, {"id": job_id, "company_id": company_id, **job})
                upsert(connection, m.routes, {"id": job_id, "job_id": job_id, "company_id": company_id, "provider": source["provider"],
                    "tenant": source["tenant"], "requisition": job["requisition"], "url": url})
                source_job_id = (job["requisition"] if batch.source_company == item.company else identity(company_id, job["requisition"]))
                posting_id = identity(source_id, source_job_id)
                raw_key = json.dumps(item.raw, sort_keys=True, ensure_ascii=False)
                if posting_id in batch_postings and batch_postings[posting_id] != raw_key:
                    raise SourceError('SOURCE_DUPLICATE_JOB_ID')
                batch_postings[posting_id] = raw_key
                persist_posting(connection, {"id": posting_id, "job_id": job_id,
                    "source": batch.endpoint, "source_job_id": source_job_id,
                    "source_url": batch.endpoint, "application_url": url,
                    "raw": item.raw, "fetched_at": timestamp})
                pending_candidates.append((posting_id, company_sources[company_id], job_id, job))
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(**({"name": batch.source_company.name} if batch.source_company else {}),
                last_success=timestamp, consecutive_failures=0, last_error=None, backoff_until=None,
                health_status="PARTIAL" if rejected else "HEALTHY", jobs_seen=len(normalized)))
            store._audit(connection, source_id, "SOURCE_COLLECTION_COMPLETE", {"jobs_seen": len(normalized), "rejected": rejected, "provider": source["provider"]})
            observation_at = now()
            observation_id = record_observation(connection, configured_source_id=source_id, observed_at=observation_at,
                source_endpoint=observed_endpoint,
                succeeded=True, available=available, enumerated=enumerated, accepted=accepted,
                completeness_sum=completeness_sum, salary_known=salary_known)
            for posting_id, company_source_id, _, values in pending_candidates:
                record_candidate(connection, posting_id=posting_id, company_source_id=company_source_id,
                                 observation_id=observation_id, normalized=values)
            for job_id in sorted({entry[2] for entry in pending_candidates}):
                resolve_job_values(connection, job_id, at=observation_at, seen_at=timestamp, profile=profile)
        return {"source_id": source_id, "jobs_seen": len(normalized), "rejected": rejected,
                "strategy": strategy, "direct_application": direct_application}
    except Exception as failure:
        # Only external fetch failures count against availability. Cancellation,
        # DB failures and local programming errors are not evidence of an outage.
        if available is None and (isinstance(failure, httpx.HTTPError) or isinstance(failure, SourceError)
                                 and (failure.code.startswith('SOURCE_HTTP_') or failure.code in {
                                     'SOURCE_RATE_LIMITED', 'SOURCE_REDIRECT_BLOCKED', 'SOURCE_TIME_LIMIT'})):
            available = False
        seconds = failure.retry_seconds if isinstance(failure, SourceError) else 60
        disabled = seconds > 31536000
        try:
            backoff = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        except OverflowError:
            backoff = datetime.max.replace(tzinfo=timezone.utc)
        with store.engine.begin() as connection:
            record_observation(connection, configured_source_id=source_id, observed_at=now(),
                source_endpoint=observed_endpoint,
                succeeded=False, available=available, enumerated=enumerated, accepted=accepted,
                completeness_sum=completeness_sum, salary_known=salary_known,
                error_code=failure.code if isinstance(failure, SourceError) else type(failure).__name__)
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
                consecutive_failures=m.job_sources.c.consecutive_failures + 1,
                health_status="RATE_LIMITED" if isinstance(failure, SourceError) and failure.code == "SOURCE_RATE_LIMITED" else "FAILED",
                last_error=failure.code if isinstance(failure, SourceError) else type(failure).__name__,
                backoff_until=backoff.isoformat(),
                **({"enabled": False} if disabled else {})))
        raise
