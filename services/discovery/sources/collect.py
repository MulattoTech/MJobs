"""Durable per-board policy and provider-independent persistence."""
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
import hashlib
import json
import httpx
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.domain.operation_errors import source_collection_error_category
from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.agents.acceptance import (
    CollectionContract, CollectionValidation, validate_collection_output,
)
from services.agents.registry import AgentToolError
from services.ai.prompt_provenance import capture_parser
from services.company_discovery.sources import register_ats_tenant, upsert_company_job_source
from services.company_discovery.quality import posting_metrics, record_observation
from services.company_discovery.requests import record_discovery_requests
from services.dedupe.source_values import lock_candidate_job, record_candidate, resolve_job_values
from services.dedupe.ingestion_identity import canonical_ingestion_job, lock_company_ingestion
from services.discovery.sources.contract import SourceAdapter, SourceCapability
from services.discovery.sources.greenhouse import Greenhouse, SourceError, tenant_identifier
from services.discovery.sources.registry import source_adapter
from services.discovery.search_semantics import normalize_reported_timestamp, parse_timestamp


class SourceCollectionBoundaryError(RuntimeError):
    """A collection failure whose private or unstable details were withheld."""


class SourceCollectionContractError(SourceError):
    """A bounded collection result needs review before it can be imported."""

    def __init__(self, stop_reason, collection_result=None):
        super().__init__("REVIEW_REQUIRED")
        self.stop_reason = stop_reason
        self.collection_result = collection_result


_SAFE_LEGACY_SOURCE_CODES = frozenset({
    "COMPANY_ATS_BINDING_REVIEW_REQUIRED",
    "COMPANY_ATS_JOB_TENANT_MISMATCH",
    "COMPANY_DOMAIN_REVIEW_REQUIRED",
    "COMPANY_SOURCE_DISABLED",
    "SOURCE_BACKOFF",
    "SOURCE_COOLDOWN",
    "SOURCE_DISABLED",
    "SOURCE_DUPLICATE_JOB_ID",
    "SOURCE_INVALID_CONTENT_TYPE",
    "SOURCE_INVALID_FORMAT",
    "SOURCE_INVALID_JOB",
    "SOURCE_INVALID_JOB_LOCATION",
    "SOURCE_INVALID_JSON",
    "SOURCE_JOB_ID_MISMATCH",
    "SOURCE_JOB_LIMIT",
    "SOURCE_JOB_NOT_FOUND",
    "SOURCE_NO_VALID_JOBS",
    "SOURCE_PAYLOAD_INVALID",
    "SOURCE_PAYLOAD_LIMIT",
    "SOURCE_ROUTE_IDENTITY_CONFLICT",
    "SOURCE_UNAVAILABLE",
    "SOURCE_UNSUPPORTED",
})

_PUBLIC_TERMINAL_SOURCE_CATEGORIES = frozenset({
    "AUTH_REQUIRED",
    "SOURCE_BLOCKED",
    "SOURCE_CHANGED",
})


def _safe_legacy_source_code(code):
    if type(code) is not str:
        return False
    return code in _SAFE_LEGACY_SOURCE_CODES


def _public_collection_failure(failure):
    if isinstance(failure, httpx.HTTPError):
        public_failure = SourceError("SOURCE_NETWORK_ERROR")
        category = "NETWORK_ERROR"
    elif isinstance(failure, SourceError):
        category = source_collection_error_category(failure.code)
        if category is None and not _safe_legacy_source_code(failure.code):
            return SourceCollectionBoundaryError("SOURCE_COLLECTION_FAILED"), None
        public_code = category if category in _PUBLIC_TERMINAL_SOURCE_CATEGORIES else failure.code
        public_failure = SourceError(public_code, retry_seconds=failure.retry_seconds)
    else:
        public_failure = SourceCollectionBoundaryError("SOURCE_COLLECTION_FAILED")
        category = None
    if category is not None:
        public_failure.operation_error_category = category
    return public_failure, category


def configure_source(store, tenant, provider="greenhouse", enabled=True, poll_interval=300, concurrency=1,
                     request_timeout=10, rate_limit=60, jitter_seconds=30, *, connection=None,
                     preserve_existing=False, registry=None):
    if provider == "remote_ok":
        provider = "remoteok"
    if provider == "greenhouse":
        tenant = tenant_identifier(tenant)
    elif provider == "lever":
        from services.discovery.sources.lever import tenant_identity
        tenant = tenant_identity(tenant)
    else:
        tenant = str(tenant or "default").strip().casefold()
    values = (poll_interval, concurrency, request_timeout, rate_limit, jitter_seconds)
    if type(enabled) is not bool or any(type(value) is not int for value in values):
        raise ValueError("Source policy values must use explicit boolean/integer types")
    if not 300 <= poll_interval <= 604800 or not 1 <= concurrency <= 8 or not 2 <= request_timeout <= 60:
        raise ValueError("Source poll interval, concurrency, or request timeout is outside supported bounds")
    if not 1 <= rate_limit <= 600 or not 0 <= jitter_seconds <= 300:
        raise ValueError("Source rate limit or jitter is outside supported bounds")
    source_id = identity(provider, tenant)
    policy = {"provider": provider, "tenant": tenant, "request_timeout": request_timeout,
              "rate_limit": rate_limit, "concurrency": concurrency}
    capabilities = source_adapter(policy, registry=registry).capabilities()
    with (nullcontext(connection) if connection is not None else store.engine.begin()) as connection:
        if preserve_existing:
            insert = {'postgresql': postgres_insert, 'sqlite': sqlite_insert}[connection.dialect.name]
            connection.execute(insert(m.job_sources).values(
                id=source_id, provider=provider, tenant=tenant, name=tenant, enabled=enabled,
                strategy=capabilities.strategy.value, capabilities=capabilities.as_dict(),
                poll_interval=poll_interval, concurrency=concurrency, request_timeout=request_timeout,
                rate_limit=rate_limit, jitter_seconds=jitter_seconds, health_status='NOT_CHECKED'
            ).on_conflict_do_nothing())
            return source_id
        upsert(connection, m.job_sources, {"id": source_id, "provider": provider, "tenant": tenant,
            "name": tenant, "enabled": enabled, "strategy": capabilities.strategy.value,
            "capabilities": capabilities.as_dict(), "poll_interval": poll_interval, "concurrency": concurrency,
            "request_timeout": request_timeout, "rate_limit": rate_limit, "jitter_seconds": jitter_seconds,
            "health_status": "NOT_CHECKED"})
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
            enabled=enabled, strategy=capabilities.strategy.value, capabilities=capabilities.as_dict(),
            poll_interval=poll_interval, concurrency=concurrency, request_timeout=request_timeout,
            rate_limit=rate_limit, jitter_seconds=jitter_seconds))
        store._audit(connection, source_id, "SOURCE_CONFIGURED", {
            "provider": provider, "tenant": tenant, "strategy": capabilities.strategy.value,
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


def _latest_valid_posted_dates(connection, posting_id):
    """Return the newest parseable date claim for one exact posting."""
    before = None
    while True:
        statement = select(
            m.posting_value_candidates.c.id,
            m.posting_value_candidates.c.normalized,
            m.posting_value_candidates.c.observed_at,
        ).where(m.posting_value_candidates.c.posting_id == posting_id)
        if before is not None:
            statement = statement.where(or_(
                m.posting_value_candidates.c.observed_at < before['observed_at'],
                and_(m.posting_value_candidates.c.observed_at == before['observed_at'],
                     m.posting_value_candidates.c.id < before['id']),
            ))
        candidates = list(connection.execute(statement.order_by(
            m.posting_value_candidates.c.observed_at.desc(),
            m.posting_value_candidates.c.id.desc(),
        ).limit(64)).mappings())
        for candidate in candidates:
            attributes = (candidate['normalized'] or {}).get('attributes') or {}
            reference = next((parsed for value in (
                attributes.get('reported_posted_at_reference_at'),
                candidate['observed_at'],
                attributes.get('last_collected_at'),
                attributes.get('first_seen_at'),
            ) if (parsed := parse_timestamp(value)) is not None), None)
            if parse_timestamp(attributes.get('reported_posted_at'), reference_at=reference) is not None:
                return {**attributes, 'observed_at': candidate['observed_at']}
        if len(candidates) < 64:
            return {}
        before = candidates[-1]


def collect_source(store, source_id, profile_id, profile_version=None, check=None, transport=None,
                   *, refresh_matches=True, import_guard=None, registry=None,
                   collection_contract=None):
    """Capture source values, optionally deferring profile scoring to the caller.

    Saved-search workers defer scoring until persisted criteria admit candidates.
    The profile still supplies discovery-request ownership during capture.
    """
    check = check or (lambda: None)
    profile = store.profile(profile_id, profile_version) if profile_id is not None else None
    check()
    timestamp = now()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        source = dict(connection.execute(select(m.job_sources).where(m.job_sources.c.id == source_id)).mappings().one())
        from services.company_discovery.ats_enrollment import bound_company_source
        bound = bound_company_source(store, connection, source, lock=True)
        source = dict(connection.execute(select(m.job_sources).where(m.job_sources.c.id == source_id).with_for_update()).mappings().one())
        if not source["enabled"]:
            raise SourceError("SOURCE_DISABLED")
        if source["backoff_until"] and source["backoff_until"] > timestamp:
            raise SourceError("SOURCE_BACKOFF")
        if source["last_attempt"] and datetime.fromisoformat(source["last_attempt"]) + timedelta(seconds=source["poll_interval"]) > datetime.now(timezone.utc):
            raise SourceError("SOURCE_COOLDOWN")
        adapter: SourceAdapter = source_adapter(
            source, transport=transport, check=check, registry=registry,
        )
        if isinstance(adapter, Greenhouse):
            adapter.set_authoritative_tenant_binding(bound is not None)
        connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(last_attempt=timestamp, health_status="COLLECTING"))
    available = None
    observed_endpoint = None
    enumerated = accepted = completeness_sum = salary_known = None
    authority_rejected = False
    try:
        if source["strategy"] == "BROWSER":
            if not callable(getattr(check, "consume_page", None)):
                raise SourceCollectionContractError("PAGE_BUDGET_REQUIRED")
            if not isinstance(collection_contract, CollectionContract):
                raise SourceCollectionContractError("COLLECTION_CONTRACT_REQUIRED")
        batch = adapter.search()
        observed_endpoint = batch.endpoint
        available = True
        collection_result = None
        enumerated = len(batch.items)
        normalized = []
        rejected = 0
        for item in batch.items:
            check()
            try:
                provenance = capture_parser(adapter)
                parsed = adapter.normalize(item.raw)
                parsed['attributes']['prompt_provenance'] = provenance
                normalized.append((item, parsed))
            except SourceError:
                rejected += 1
        if collection_contract is not None:
            collection_output = {
                # Completeness anchors describe the provider batch, not only
                # the subset that survived local normalization.
                "items": [item.raw for item in batch.items],
            }
            if collection_contract.count_key is not None and batch.complete_inventory:
                # A declared complete inventory makes the enumerated batch size
                # authoritative. Never synthesize provider count evidence from
                # the subset that happened to normalize successfully.
                collection_output[collection_contract.count_key] = len(batch.items)
            collection_result = validate_collection_output(collection_output, collection_contract)
            if collection_result.code == "OUTPUT_VALID" and rejected:
                collection_result = CollectionValidation(
                    "STRUCTURE_INVALID", len(batch.items), None,
                )
            if collection_result.code != "OUTPUT_VALID":
                raise SourceCollectionContractError(collection_result.code, collection_result)
        if bound:
            for _, job in normalized:
                url = job.get('application_url')
                if source['provider']=='lever':
                    from services.discovery.sources.lever import listing_url
                    valid = listing_url(source['tenant'],url,identifier=job['requisition']) is not None
                else:
                    valid = not url or urlsplit(url).path.split('/')[:2] == ['',source['tenant']]
                if not valid:
                    raise SourceError('COMPANY_ATS_JOB_TENANT_MISMATCH')
        accepted = len(normalized)
        metrics = [posting_metrics(job, job.get('application_url')) for _, job in normalized]
        completeness_sum = sum(metric[0] for metric in metrics)
        salary_known = sum(int(metric[1]) for metric in metrics)
        if batch.items and not normalized:
            raise SourceError("SOURCE_NO_VALID_JOBS")
        capabilities = adapter.capabilities()
        strategy = capabilities.strategy.value
        direct_application = capabilities.supports(SourceCapability.DIRECT_APPLICATION)
        with store.engine.begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            # Reauthorize in the shared ingestion/domain/source lock order.
            try:
                fresh_bound = bound_company_source(store, connection, source, lock=True)
                if import_guard:
                    import_guard(connection)
                current_source = connection.execute(select(m.job_sources).where(
                    m.job_sources.c.id == source_id).with_for_update()).mappings().one()
                if not current_source['enabled']:
                    raise SourceError('SOURCE_DISABLED')
                if any(current_source[key] != source[key] for key in ('provider', 'tenant')):
                    raise SourceError('SOURCE_CHANGED')
                if (fresh_bound or {}).get('id') != (bound or {}).get('id'):
                    raise SourceError('COMPANY_ATS_BINDING_REVIEW_REQUIRED')
            except Exception:
                authority_rejected = True
                raise
            bound = fresh_bound
            def company_identity(company):
                return bound['company_id'] if bound else identity(*company.identity_parts)
            check()
            companies = {}
            new_companies = set()
            company_sources = {}
            pending_candidates = []
            batch_postings = {}
            batch_identities = {}
            for company in ([batch.source_company] if batch.source_company else []) + [item.company for item, _ in normalized]:
                company_id = company_identity(company)
                companies[company_id] = company
            lock_company_ingestion(connection, companies)
            for company_id, company in sorted(companies.items()):
                existing_company = connection.execute(select(m.companies).where(
                    m.companies.c.id == company_id
                )).mappings().one_or_none()
                if existing_company is None:
                    new_companies.add(company_id)
                first_party_ats = source["provider"] in {"greenhouse","lever"}
                # Aggregator adapters identify the listing origin, not an employer
                # website or employer-specific ATS tenant. Keep that distinction in
                # company records even when the feed attributes a posting to a company.
                upsert(connection, m.companies, {
                    "id": company_id,
                    "name": company.name if not bound and (first_party_ats or existing_company is None) else existing_company["name"],
                    "domain": company.domain if first_party_ats and not bound else (
                        existing_company["domain"] if existing_company else ""
                    ),
                }, replace=True)
                if bound:
                    connection.execute(update(m.company_sources).where(m.company_sources.c.id == bound['id'])
                                       .values(last_validation=timestamp, last_success=timestamp))
                    company_sources[company_id] = bound['id']
                    continue
                tenant_record = None
                if first_party_ats:
                    tenant_record = register_ats_tenant(
                        connection, company_id, provider=source["provider"], tenant_identifier=source["tenant"],
                        confidence=0.9, evidence=["configured public job source adapter"],
                    )
                company_source = upsert_company_job_source(
                    connection, company_id=company_id, provider=source["provider"], tenant_identifier=source["tenant"],
                    public_api_url=batch.endpoint if strategy in {"API", "PUBLIC_JSON"} else None,
                    feed_url=batch.endpoint if strategy == "FEED" else None,
                    search_url=batch.endpoint if strategy in {"PUBLIC_HTML", "BROWSER"} else None,
                    collection_strategy=strategy, confidence=0.9 if tenant_record else 0.45, enabled=True,
                    priority="NORMAL", discovered_automatically=False,
                    ats_tenant_id=tenant_record["id"] if tenant_record else None,
                    last_validation=timestamp, last_success=timestamp,
                )
                connection.execute(update(m.company_sources).where(m.company_sources.c.id == company_source['id'])
                                   .values(collector_source_id=source_id))
                company_sources[company_id] = company_source['id']
            for item, job in sorted(normalized, key=lambda pair: (company_identity(pair[0].company), pair[1]['requisition'])):
                company_id = company_identity(item.company)
                native_job_id = identity(company_id, source["provider"], source["tenant"], job["requisition"])
                url = job.pop("application_url")
                source_job_id = (job["requisition"] if batch.source_company == item.company else identity(company_id, job["requisition"]))
                posting_id = identity(source_id, source_job_id)
                canonical = canonical_ingestion_job(connection, company_id=company_id,
                    proposed_id=native_job_id, posting_id=posting_id, application_url=url,
                    provider=source['provider'], tenant=source['tenant'], requisition=job['requisition'],
                    batch_identities=batch_identities)
                job_id = canonical['id'] if canonical else native_job_id
                batch_identities.setdefault(job_id, []).append((source['provider'], source['tenant'], job['requisition']))
                existing_attributes = connection.execute(select(m.jobs.c.attributes).where(m.jobs.c.id == job_id)).scalar_one_or_none()
                existing_attributes = dict(existing_attributes or {})
                # The anchor belongs to this posting, not another canonical source
                # or a later poll. Prior valid snapshots carry the stable reference
                # while still allowing an explicit absolute source correction.
                previous_dates = _latest_valid_posted_dates(connection, posting_id) or (
                    existing_attributes if existing_attributes.get('source_id') == source_id else {})
                attrs = job['attributes']
                raw_date = attrs.get('reported_posted_at_raw', attrs.get('reported_posted_at'))
                # Adapters may validate another native field (RemoteOK epoch).
                # A missing/malformed display date must not erase that absolute
                # timestamp; keep the display claim and raw posting as provenance.
                absolute_date = parse_timestamp(attrs.get('reported_posted_at'))
                dates = normalize_reported_timestamp(
                    absolute_date.isoformat() if absolute_date else raw_date,
                    reference_at=timestamp, previous=previous_dates)
                if absolute_date:
                    dates['reported_posted_at_raw'] = raw_date
                attrs.update(dates)
                job["attributes"].update(
                    provider=source['provider'], tenant=source['tenant'],
                    source_id=source_id,
                    source_endpoint=batch.endpoint,
                    first_seen_at=existing_attributes.get("first_seen_at") or timestamp,
                    last_seen_at=timestamp,
                    last_collected_at=timestamp,
                )
                lock_candidate_job(connection, {"id": job_id, "company_id": company_id, **job,
                    'requisition': canonical['requisition'] if canonical else job['requisition']})
                # Attaching an authorized first-party posting to an existing job
                # retains its exact tenant route as an alias. Preserve the old
                # route and job identity; verified ATS filters need this binding.
                existing_route = None
                if job_id == native_job_id or bound:
                    existing_route = connection.execute(select(m.routes).where(
                        m.routes.c.company_id == company_id, m.routes.c.provider == source['provider'],
                        m.routes.c.tenant == source['tenant'], m.routes.c.requisition == job['requisition']
                    )).mappings().one_or_none()
                    if existing_route and existing_route['job_id'] != job_id:
                        raise SourceError('SOURCE_ROUTE_IDENTITY_CONFLICT')
                if (job_id == native_job_id or bound) and existing_route is None:
                    upsert(connection, m.routes, {"id": native_job_id, "job_id": job_id, "company_id": company_id, "provider": source["provider"],
                        "tenant": source["tenant"], "requisition": job["requisition"], "url": url})
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
                resolve_job_values(connection, job_id, at=observation_at, seen_at=timestamp,
                                   profile=profile if refresh_matches else None)
            if batch.complete_inventory and not rejected:
                from services.discovery.lifecycle import reconcile_inventory
                reconcile_inventory(connection, endpoint=batch.endpoint, posting_ids=set(batch_postings),
                                    observation_id=observation_id)
            if profile is not None:
                record_discovery_requests(connection, {entry[2] for entry in pending_candidates}, profile,
                                          new_company_ids=new_companies)
        return {"source_id": source_id, "jobs_seen": len(normalized), "rejected": rejected,
                "strategy": strategy, "direct_application": direct_application,
                "collection_result": ({
                    "code": collection_result.code,
                    "observed_count": collection_result.observed_count,
                    "tail_matches": collection_result.tail_matches,
                } if collection_result is not None else None)}
    except Exception as failure:
        from workers.operations import OperationCancelled, OperationTimedOut
        if isinstance(failure, AgentToolError) and failure.code == "PAGE_BUDGET_EXHAUSTED":
            failure = SourceCollectionContractError(failure.code)
        if authority_rejected or isinstance(
                failure, (OperationCancelled, OperationTimedOut, SourceCollectionContractError)):
            # A local authority revocation or budget stop is not a provider failure.
            # Restore the last observed health and failure metadata instead of
            # poisoning source availability or imposing provider backoff.
            with store.engine.begin() as connection:
                connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
                    health_status=source["health_status"],
                    consecutive_failures=source["consecutive_failures"],
                    last_error=source["last_error"],
                    backoff_until=source["backoff_until"],
                ))
            if authority_rejected and isinstance(failure, SourceError):
                category = source_collection_error_category(failure.code)
                if category is not None:
                    failure.operation_error_category = category
            if isinstance(failure, SourceCollectionContractError):
                raise failure from None
            raise
        provider_boundary = available is None
        if provider_boundary:
            public_failure, operation_category = _public_collection_failure(failure)
            source_health_error = (failure.code if isinstance(failure, SourceError)
                                       and (operation_category is not None
                                            or _safe_legacy_source_code(failure.code))
                                       else operation_category)
        else:
            # Once a fetch has completed, failures belong to local normalization,
            # authorization, or persistence. Keep their transactional exception
            # semantics while persisting only a stable code/type, never its text.
            public_failure = failure
            operation_category = (source_collection_error_category(failure.code)
                                  if isinstance(failure, SourceError) else None)
            if operation_category is not None:
                failure.operation_error_category = operation_category
            source_health_error = (failure.code if isinstance(failure, SourceError)
                                   and (operation_category is not None
                                        or _safe_legacy_source_code(failure.code))
                                   else type(failure).__name__)
        # Only external fetch failures count against availability. Cancellation,
        # DB failures and local programming errors are not evidence of an outage.
        if available is None and (isinstance(failure, httpx.HTTPError) or isinstance(failure, SourceError)
                                 and type(failure.code) is str
                                 and (failure.code.startswith('SOURCE_HTTP_') or failure.code in {
                                     'SOURCE_RATE_LIMITED', 'SOURCE_REDIRECT_BLOCKED', 'SOURCE_TIME_LIMIT',
                                     'SOURCE_TIMEOUT', 'SOURCE_NETWORK_ERROR', 'SOURCE_SERVER_ERROR'})):
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
                error_code=source_health_error)
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
                consecutive_failures=m.job_sources.c.consecutive_failures + 1,
                health_status="RATE_LIMITED" if operation_category == "RATE_LIMITED" else "FAILED",
                last_error=source_health_error,
                backoff_until=backoff.isoformat(),
                **({"enabled": False} if disabled else {})))
            if operation_category is not None:
                store._audit(connection, source_id, "SOURCE_COLLECTION_FAILED", {
                    "error_category": operation_category,
                    "provider": source["provider"],
                })
        if public_failure is failure:
            raise
        deferred_public_failure = public_failure
    raise deferred_public_failure from None
