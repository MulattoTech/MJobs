"""Durable per-board policy and provider-independent persistence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
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
    upsert(connection, m.postings, data, replace=True)
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
    try:
        batch = adapter.search()
        normalized = []
        rejected = 0
        for item in batch.items:
            check()
            try:
                normalized.append((item, adapter.normalize(item.raw)))
            except SourceError:
                rejected += 1
        if batch.items and not normalized:
            raise SourceError("SOURCE_NO_VALID_JOBS")
        capabilities = adapter.capabilities()
        strategy = capabilities.strategy.value
        direct_application = capabilities.supports(SourceCapability.DIRECT_APPLICATION)
        usable = {fact["text"].strip().casefold() for fact in profile["facts"] if fact["kind"] == "skill"
                  and fact["verified"] and fact["user_approved"] and fact["source"].strip() and fact["confidence"] >= 80}
        with store.engine.begin() as connection:
            check()
            companies = {}
            for company in ([batch.source_company] if batch.source_company else []) + [item.company for item, _ in normalized]:
                company_id = identity(*company.identity_parts)
                companies[company_id] = company
            for company_id, company in companies.items():
                upsert(connection, m.companies, {"id": company_id, "name": company.name, "domain": company.domain}, replace=True)
                upsert(connection, m.company_sources, {"id": identity(company_id, source["provider"], source["tenant"]), "company_id": company_id,
                    "provider": source["provider"], "tenant": source["tenant"], "endpoint": batch.endpoint,
                    "discovered_automatically": False})
            for item, job in normalized:
                company_id = identity(*item.company.identity_parts)
                job_id = identity(company_id, source["provider"], source["tenant"], job["requisition"])
                url = job.pop("application_url")
                existing_attributes = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one_or_none()
                existing_attributes = dict(existing_attributes or {})
                if not job["attributes"].get("reported_posted_at") and existing_attributes.get("reported_posted_at"):
                    job["attributes"]["reported_posted_at"] = existing_attributes["reported_posted_at"]
                job["attributes"].update(
                    source_id=source_id,
                    source_endpoint=batch.endpoint,
                    first_seen_at=existing_attributes.get("first_seen_at") or timestamp,
                    last_seen_at=timestamp,
                    last_collected_at=timestamp,
                )
                upsert(connection, m.jobs, {"id": job_id, "company_id": company_id, **job}, replace=True)
                upsert(connection, m.routes, {"id": job_id, "job_id": job_id, "company_id": company_id, "provider": source["provider"],
                    "tenant": source["tenant"], "requisition": job["requisition"], "url": url}, replace=True)
                source_job_id = (job["requisition"] if batch.source_company == item.company else identity(company_id, job["requisition"]))
                persist_posting(connection, {"id": identity(source_id, source_job_id), "job_id": job_id,
                    "source": batch.endpoint, "source_job_id": source_job_id, "raw": item.raw, "fetched_at": timestamp})
                mentions = {skill.casefold() for skill in job["skills"]}
                score = round(100 * len(mentions & usable) / len(mentions), 2) if mentions else 0
                upsert(connection, m.matches, {"id": identity(job_id, profile_id, profile["version"]), "job_id": job_id,
                    "profile_id": profile_id, "profile_version": profile["version"], "score": score, "missing": sorted(mentions - usable),
                    "components": {"method": "description_skill_mentions", "matched": sorted(mentions & usable),
                        "detected": sorted(mentions), "limitation": "Skill mentions are not verified mandatory requirements or a full qualification assessment."}}, replace=True)
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(**({"name": batch.source_company.name} if batch.source_company else {}),
                last_success=timestamp, consecutive_failures=0, last_error=None, backoff_until=None,
                health_status="PARTIAL" if rejected else "HEALTHY", jobs_seen=len(normalized)))
            store._audit(connection, source_id, "SOURCE_COLLECTION_COMPLETE", {"jobs_seen": len(normalized), "rejected": rejected, "provider": source["provider"]})
        return {"source_id": source_id, "jobs_seen": len(normalized), "rejected": rejected,
                "strategy": strategy, "direct_application": direct_application}
    except Exception as failure:
        seconds = failure.retry_seconds if isinstance(failure, SourceError) else 60
        disabled = seconds > 31536000
        try:
            backoff = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        except OverflowError:
            backoff = datetime.max.replace(tzinfo=timezone.utc)
        with store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
                consecutive_failures=m.job_sources.c.consecutive_failures + 1,
                health_status="RATE_LIMITED" if isinstance(failure, SourceError) and failure.code == "SOURCE_RATE_LIMITED" else "FAILED",
                last_error=failure.code if isinstance(failure, SourceError) else type(failure).__name__,
                backoff_until=backoff.isoformat(),
                **({"enabled": False} if disabled else {})))
        raise
