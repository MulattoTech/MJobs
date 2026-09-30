"""Durable per-board policy and provider-independent persistence."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.discovery.sources.contract import SourceAdapter
from services.discovery.sources.greenhouse import Greenhouse, SourceError, tenant_identifier


def configure_source(store, tenant, enabled=True, poll_interval=300):
    tenant = tenant_identifier(tenant)
    if type(enabled) is not bool or type(poll_interval) is not int or not 300 <= poll_interval <= 86400:
        raise ValueError("Source enabled must be boolean; collection interval must be 300 through 86400 seconds")
    source_id = identity("greenhouse", tenant)
    with store.engine.begin() as connection:
        upsert(connection, m.job_sources, {"id": source_id, "provider": "greenhouse", "tenant": tenant,
            "name": tenant, "enabled": enabled, "strategy": "API", "poll_interval": poll_interval,
            "health_status": "NOT_CHECKED"})
        # Preserve attempts, provider backoff and failures on ordinary configuration.
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(enabled=enabled, poll_interval=poll_interval))
        store._audit(connection, source_id, "SOURCE_CONFIGURED", {"provider": "greenhouse", "tenant": tenant})
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
        if source["provider"] != "greenhouse":
            raise SourceError("SOURCE_UNSUPPORTED")
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(last_attempt=timestamp, health_status="COLLECTING"))
    try:
        adapter: SourceAdapter = Greenhouse(source["tenant"], transport=transport, check=check)
        batch = adapter.collect()
        normalized = []
        rejected = 0
        for raw in batch["jobs"]:
            check()
            try:
                normalized.append((raw, adapter.normalize(raw)))
            except SourceError:
                rejected += 1
        if batch["jobs"] and not normalized:
            raise SourceError("SOURCE_NO_VALID_JOBS")
        usable = {fact["text"].strip().casefold() for fact in profile["facts"] if fact["kind"] == "skill"
                  and fact["verified"] and fact["user_approved"] and fact["source"].strip() and fact["confidence"] >= 80}
        company_id = identity("greenhouse", source["tenant"])
        with store.engine.begin() as connection:
            check()
            upsert(connection, m.companies, {"id": company_id, "name": batch["company"], "domain": "https://job-boards.greenhouse.io/" + source["tenant"]}, replace=True)
            upsert(connection, m.company_sources, {"id": identity(company_id, "greenhouse", source["tenant"]), "company_id": company_id,
                "provider": "greenhouse", "tenant": source["tenant"], "endpoint": batch["endpoint"], "discovered_automatically": False})
            for raw, job in normalized:
                job_id = identity(company_id, "greenhouse", source["tenant"], job["requisition"])
                url = job.pop("application_url")
                job["attributes"].update(source_id=source_id, source_endpoint=batch["endpoint"], last_collected_at=timestamp)
                upsert(connection, m.jobs, {"id": job_id, "company_id": company_id, **job}, replace=True)
                upsert(connection, m.routes, {"id": job_id, "job_id": job_id, "company_id": company_id, "provider": "greenhouse",
                    "tenant": source["tenant"], "requisition": job["requisition"], "url": url}, replace=True)
                persist_posting(connection, {"id": identity(source_id, job["requisition"]), "job_id": job_id,
                    "source": batch["endpoint"], "source_job_id": job["requisition"], "raw": raw, "fetched_at": timestamp})
                mentions = {skill.casefold() for skill in job["skills"]}
                score = round(100 * len(mentions & usable) / len(mentions), 2) if mentions else 0
                upsert(connection, m.matches, {"id": identity(job_id, profile_id, profile["version"]), "job_id": job_id,
                    "profile_id": profile_id, "profile_version": profile["version"], "score": score, "missing": sorted(mentions - usable),
                    "components": {"method": "description_skill_mentions", "matched": sorted(mentions & usable),
                        "detected": sorted(mentions), "limitation": "Skill mentions are not verified mandatory requirements or a full qualification assessment."}}, replace=True)
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(name=batch["company"],
                last_success=timestamp, consecutive_failures=0, last_error=None, backoff_until=None,
                health_status="PARTIAL" if rejected else "HEALTHY", jobs_seen=len(normalized)))
            store._audit(connection, source_id, "SOURCE_COLLECTION_COMPLETE", {"jobs_seen": len(normalized), "rejected": rejected, "provider": "greenhouse"})
        return {"source_id": source_id, "jobs_seen": len(normalized), "rejected": rejected, "strategy": "API", "direct_application": False}
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
