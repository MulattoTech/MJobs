"""Durable company ATS tenant/source records and explicit company hierarchy."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import math
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, or_, select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, now, upsert
from services.audit_history import record_company_discovered, record_company_source_enrolled
from services.company_discovery.source_host_locks import lock_company_source_hosts


_PRIORITIES = {"USER_PINNED", "HIGH", "NORMAL", "LOW", "DORMANT"}
_COLLECTION_STRATEGIES = {
    "UNKNOWN", "API", "FEED", "PUBLIC_JSON", "PUBLIC_HTML", "ASSISTED", "MANUAL", "DISABLED",
    "PUBLIC_API", "RSS", "STRUCTURED_JSON", "SEARCH_PAGE", "HTML", "BROWSER", "FIXTURE",
}
_UNSET = object()


def _token(value, name, maximum, *, required=True, fold=False):
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"COMPANY_SOURCE_{name}_REQUIRED")
    if len(result) > maximum:
        raise ValueError(f"COMPANY_SOURCE_{name}_INVALID")
    return result.casefold() if fold else result


def _confidence(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("COMPANY_SOURCE_CONFIDENCE_INVALID")
    return float(value)


def _url(value, name, *, required=False):
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError(f"COMPANY_SOURCE_{name}_REQUIRED")
        return None
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None and len(raw) <= 2000)
        port = parsed.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"COMPANY_SOURCE_{name}_INVALID")
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold().rstrip(".")
    if ":" in hostname:  # IPv6 literals require brackets in the authority.
        hostname = f"[{hostname}]"
    if port is not None and not ((scheme == "http" and port == 80)
                                 or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, hostname, path, parsed.query, ""))


def register_ats_tenant(store, company_id, *, provider, tenant_identifier,
                        organization_identifier="", board_identifier="", confidence=0.45,
                        evidence=()):
    """Persist a provider tenant with evidence and monotonic confidence."""
    company_id = _token(company_id, "COMPANY_ID", 128)
    provider = _token(provider, "PROVIDER", 32, fold=True)
    tenant = _token(tenant_identifier, "TENANT", 128)
    organization = _token(organization_identifier, "ORGANIZATION", 128, required=False)
    board = _token(board_identifier, "BOARD", 128, required=False)
    confidence = _confidence(confidence)
    if not isinstance(evidence, (list, tuple)) or any(not isinstance(item, str) for item in evidence):
        raise ValueError("COMPANY_SOURCE_EVIDENCE_INVALID")
    evidence = list(dict.fromkeys(item.strip()[:1000] for item in evidence if item.strip()))[:50]
    timestamp = now()
    tenant_id = identity(company_id, provider, tenant, organization, board)
    context = store.engine.begin() if isinstance(store, Store) else nullcontext(store)
    with context as connection:
        if connection.execute(select(m.companies.c.id).where(m.companies.c.id == company_id)).first() is None:
            raise ValueError("COMPANY_SOURCE_COMPANY_NOT_FOUND")
        existing = connection.execute(select(m.company_ats_tenants).where(
            m.company_ats_tenants.c.company_id == company_id,
            m.company_ats_tenants.c.provider == provider,
            m.company_ats_tenants.c.tenant_identifier == tenant,
            m.company_ats_tenants.c.organization_identifier == organization,
            m.company_ats_tenants.c.board_identifier == board,
        )).mappings().one_or_none()
        if existing:
            merged_evidence = list(dict.fromkeys([*existing["evidence"], *evidence]))[:50]
            connection.execute(update(m.company_ats_tenants).where(
                m.company_ats_tenants.c.id == existing["id"]
            ).values(confidence=max(float(existing["confidence"]), confidence),
                     evidence=merged_evidence, last_seen_at=timestamp))
            return dict(connection.execute(select(m.company_ats_tenants).where(
                m.company_ats_tenants.c.id == existing["id"]
            )).mappings().one())
        upsert(connection, m.company_ats_tenants, {
            "id": tenant_id, "company_id": company_id, "provider": provider,
            "tenant_identifier": tenant, "organization_identifier": organization,
            "board_identifier": board, "confidence": confidence, "evidence": evidence,
            "first_seen_at": timestamp, "last_seen_at": timestamp,
        })
        return dict(connection.execute(select(m.company_ats_tenants).where(
            m.company_ats_tenants.c.id == tenant_id
        )).mappings().one())


def upsert_company_job_source(store, *, company_id, provider, tenant_identifier,
                              career_url=None, search_url=None, feed_url=None, public_api_url=None,
                              collection_strategy="UNKNOWN", confidence=0.45, enabled=True,
                              priority="NORMAL", discovered_automatically=False, ats_tenant_id=None,
                              last_validation=None, last_success=_UNSET, profile_id=None):
    """Create or update one source portal without collapsing sibling portals."""
    company_id = _token(company_id, "COMPANY_ID", 128)
    provider = _token(provider, "PROVIDER", 32, fold=True)
    tenant = _token(tenant_identifier, "TENANT", 128)
    career_url = _url(career_url, "CAREER_URL")
    search_url = _url(search_url, "SEARCH_URL")
    feed_url = _url(feed_url, "FEED_URL")
    public_api_url = _url(public_api_url, "PUBLIC_API_URL")
    endpoint = public_api_url or feed_url or search_url or career_url
    if endpoint is None:
        raise ValueError("COMPANY_SOURCE_ENDPOINT_REQUIRED")
    strategy = _token(collection_strategy, "COLLECTION_STRATEGY", 32, fold=True).upper()
    if strategy not in _COLLECTION_STRATEGIES:
        raise ValueError("COMPANY_SOURCE_COLLECTION_STRATEGY_INVALID")
    confidence = _confidence(confidence)
    if type(enabled) is not bool or type(discovered_automatically) is not bool:
        raise ValueError("COMPANY_SOURCE_BOOLEAN_INVALID")
    priority = _token(priority, "PRIORITY", 32, fold=True).upper()
    if priority not in _PRIORITIES:
        raise ValueError("COMPANY_SOURCE_PRIORITY_INVALID")
    last_validation = _token(last_validation, "LAST_VALIDATION", 64, required=False) or None
    if last_success is not _UNSET:
        last_success = _token(last_success, "LAST_SUCCESS", 64, required=False) or None
    ats_tenant_id = _token(ats_tenant_id, "ATS_TENANT_ID", 128, required=False) or None
    # The portal URL is part of the deterministic identity, so multiple portals
    # for one company/ATS tenant remain distinct durable sources.
    context = store.engine.begin() if isinstance(store, Store) else nullcontext(store)
    with context as connection:
        # Serialize source writes against reviewed-domain owner scans. Acquire
        # all URL host suffixes before reading or changing any portal row.
        lock_company_source_hosts(connection, (endpoint, career_url, search_url, feed_url, public_api_url))
        if connection.execute(select(m.companies.c.id).where(m.companies.c.id == company_id)).first() is None:
            raise ValueError("COMPANY_SOURCE_COMPANY_NOT_FOUND")
        if ats_tenant_id:
            tenant_row = connection.execute(select(m.company_ats_tenants).where(
                m.company_ats_tenants.c.id == ats_tenant_id
            )).mappings().one_or_none()
            if (tenant_row is None or tenant_row["company_id"] != company_id
                    or tenant_row["provider"] != provider or tenant_row["tenant_identifier"] != tenant):
                raise ValueError("COMPANY_SOURCE_ATS_TENANT_MISMATCH")
        # A pre-migration source has no portal URL. Reuse one legacy record for
        # its first validation so schema expansion does not duplicate it; later
        # portals receive distinct identities based on their visible URL.
        legacy = None
        if career_url is None:
            # A legacy row has no portal identity. Reuse it only when the new
            # source also lacks a career URL and its endpoint matches exactly;
            # otherwise retain it and create a distinct portal record.
            legacy = connection.execute(select(m.company_sources.c.id).where(
                m.company_sources.c.company_id == company_id,
                m.company_sources.c.provider == provider,
                m.company_sources.c.tenant == tenant,
                m.company_sources.c.career_url.is_(None),
                m.company_sources.c.endpoint == endpoint,
            ).order_by(m.company_sources.c.id).limit(1)).first()
        source_id = legacy.id if legacy else identity(company_id, provider, tenant, career_url or endpoint)
        existing = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id
        )).mappings().one_or_none()
        origin = "AUTO_DISCOVERED" if discovered_automatically else "USER_WATCHED"
        if existing:
            if last_success is _UNSET:
                last_success = existing["last_success"]
            if discovered_automatically and existing["origin"] == "USER_WATCHED":
                # Automated rediscovery refreshes source facts but cannot silently
                # erase a user's watch preference or reprioritize that source.
                origin = "USER_WATCHED"
                priority = existing["priority"]
            if discovered_automatically:
                enabled = existing["enabled"]
                if existing["priority"] == "USER_PINNED":
                    priority = "USER_PINNED"
        elif last_success is _UNSET:
            last_success = None
        upsert(connection, m.company_sources, {
            "id": source_id, "company_id": company_id, "provider": provider,
            "tenant": tenant, "endpoint": endpoint,
            "discovered_automatically": discovered_automatically,
            "origin": origin,
            "ats_tenant_id": ats_tenant_id, "career_url": career_url,
            "search_url": search_url, "feed_url": feed_url, "public_api_url": public_api_url,
            "collection_strategy": strategy, "confidence": confidence, "enabled": enabled,
            "priority": priority, "last_validation": last_validation, "last_success": last_success,
        }, replace=True)
        if profile_id:
            record_company_discovered(connection, profile_id, company_id)
            if not existing:
                record_company_source_enrolled(connection, profile_id, company_id, source_id)
        return dict(connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id
        )).mappings().one())


def list_company_job_sources(store, company_id=None):
    """Return durable employer sources, optionally scoped to one company."""
    context = store.engine.connect() if isinstance(store, Store) else nullcontext(store)
    with context as connection:
        query = select(m.company_sources).order_by(m.company_sources.c.company_id,
                                                    m.company_sources.c.provider,
                                                    m.company_sources.c.tenant,
                                                    m.company_sources.c.id)
        if company_id:
            query = query.where(m.company_sources.c.company_id == _token(company_id, "COMPANY_ID", 128))
        return [dict(row) for row in connection.execute(query).mappings()]


def _record_enrolled_profile_setting_changes(store, connection, source_id):
    enrolled_rows = connection.execute(select(
        m.audit.c.entity_id, m.audit.c.payload
    ).where(m.audit.c.action == "COMPANY_SOURCE_ENROLLED")).mappings()
    profile_ids = {
        row["entity_id"] for row in enrolled_rows
        if row["entity_id"] and isinstance(row["payload"], dict)
        and row["payload"].get("company_source_id") == source_id
    }
    for profile_id in profile_ids:
        store._audit(connection, profile_id, "SETTING_CHANGED", {})


def set_company_source_watch(store, source_id, *, watched):
    """Explicitly set source watch state; auto refreshes preserve watched priority."""
    source_id = _token(source_id, "SOURCE_ID", 128)
    if type(watched) is not bool:
        raise ValueError("COMPANY_SOURCE_WATCH_INVALID")
    context = store.engine.begin() if isinstance(store, Store) else nullcontext(store)
    with context as connection:
        if connection.dialect.name == "sqlite" and isinstance(store, Store):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id
        ).with_for_update()).mappings().one_or_none()
        if current is None:
            raise ValueError("COMPANY_SOURCE_NOT_FOUND")
        origin = ("USER_WATCHED" if watched or not current["discovered_automatically"]
                  else "AUTO_DISCOVERED")
        if current["origin"] != origin:
            connection.execute(update(m.company_sources).where(
                m.company_sources.c.id == source_id
            ).values(origin=origin))
            _record_enrolled_profile_setting_changes(store, connection, source_id)
        return dict(connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id
        )).mappings().one())


def link_company_parent(store, parent_company_id, child_company_id, relationship_type):
    """Record a hierarchy edge without merging either company identity."""
    parent_id = _token(parent_company_id, "PARENT_COMPANY_ID", 128)
    child_id = _token(child_company_id, "CHILD_COMPANY_ID", 128)
    relationship = _token(relationship_type, "RELATIONSHIP_TYPE", 32, fold=True).upper()
    if parent_id == child_id or not re.fullmatch(r"[A-Z][A-Z0-9_]*", relationship):
        raise ValueError("COMPANY_PARENT_RELATIONSHIP_INVALID")
    context = store.engine.begin() if isinstance(store, Store) else nullcontext(store)
    with context as connection:
        if connection.dialect.name == "postgresql":
            # Serialize hierarchy writes so concurrent requests cannot each
            # validate against a stale ancestor chain and jointly form a cycle.
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(718349201)")
        elif connection.dialect.name == "sqlite" and isinstance(store, Store):
            # BEGIN IMMEDIATE reserves SQLite's single writer before reading
            # the parent chain; this connection has not issued SQL yet.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        existing = set(connection.execute(select(m.companies.c.id).where(
            m.companies.c.id.in_([parent_id, child_id])
        )).scalars())
        if existing != {parent_id, child_id}:
            raise ValueError("COMPANY_PARENT_COMPANY_NOT_FOUND")
        current = parent_id
        visited = set()
        while current is not None:
            if current == child_id or current in visited:
                raise ValueError("COMPANY_PARENT_RELATIONSHIP_INVALID")
            visited.add(current)
            current = connection.execute(select(m.companies.c.parent_company_id).where(
                m.companies.c.id == current
            )).scalar_one_or_none()
        connection.execute(update(m.companies).where(m.companies.c.id == child_id).values(
            parent_company_id=parent_id, relationship_type=relationship,
        ))
        return {"parent_company_id": parent_id, "child_company_id": child_id,
                "relationship_type": relationship}


def _set_source_control(store, source_id, column, value, action):
    """Set an explicit user choice once, preserving unrelated source fields."""
    source_id = _token(source_id, "SOURCE_ID", 128)
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id).with_for_update()).mappings().one_or_none()
        if current is None:
            raise ValueError("COMPANY_SOURCE_NOT_FOUND")
        if current[column] != value:
            connection.execute(update(m.company_sources).where(
                m.company_sources.c.id == source_id).values(**{column: value}))
            store._audit(connection, source_id, action, {
                "company_source_id": source_id, "previous": current[column], "value": value,
            })
            _record_enrolled_profile_setting_changes(store, connection, source_id)
            if column == 'enabled':
                connection.execute(update(m.company_source_monitoring).where(m.company_source_monitoring.c.source_id == source_id)
                    .values(revision=m.company_source_monitoring.c.revision+1, status='IDLE' if value else 'BLOCKED',
                            next_due=now(), last_error=None if value else 'MONITOR_SOURCE_DISABLED', updated_at=now()))
        return dict(connection.execute(select(m.company_sources).where(
            m.company_sources.c.id == source_id)).mappings().one())


def set_company_source_pin(store, source_id, *, pinned):
    if type(pinned) is not bool:
        raise ValueError("COMPANY_SOURCE_PIN_INVALID")
    return _set_source_control(store, source_id, "priority", "USER_PINNED" if pinned else "NORMAL",
                               "COMPANY_SOURCE_PIN_CHANGED")


def set_company_source_enabled(store, source_id, *, enabled):
    if type(enabled) is not bool:
        raise ValueError("COMPANY_SOURCE_ENABLED_INVALID")
    return _set_source_control(store, source_id, "enabled", enabled, "COMPANY_SOURCE_ENABLED_CHANGED")


def _view_url(value):
    """No network access. Exclude user-info, query strings and fragments from view evidence."""
    try:
        parsed = urlsplit(str(value or "")[:2000])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        parsed.port
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
    except ValueError:
        return None


def _view_event(row):
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    fields = ("company_source_id", "discovery_method", "provider", "tenant", "collection_strategy",
              "strategy", "jobs_enumerated", "jobs_imported", "matched_jobs", "evaluated_profile_count", "resolution_confidence", "status", "error_code")
    details = {}
    for key in fields:
        value = payload.get(key)
        if isinstance(value, str):
            details[key] = value[:250]
        elif type(value) in (int, float) and math.isfinite(value):
            details[key] = value
    for key in ("endpoint", "careers_url", "homepage_url", "search_result_url"):
        if value := _view_url(payload.get(key)):
            details[key] = value
    return {"id": row["id"], "action": row["action"], "timestamp": row["timestamp"], "details": details}


def list_company_source_view(store, profile_id=None):
    """Project persisted evidence without fabricating official identity or scheduling."""
    from services.company_discovery.quality import company_source_quality
    profile = store.profile(profile_id) if profile_id is not None else None
    with store.engine.connect() as c:
        profile_company_ids = None
        if profile_id is not None:
            profile_company_ids = set(c.execute(select(
                m.audit.c.payload["company_id"].as_string()).where(
                    m.audit.c.entity_id == profile_id,
                    m.audit.c.action.in_(("COMPANY_DISCOVERED", "COMPANY_SOURCE_ENROLLED")),
                )).scalars())
        rows_query = select(m.company_sources, m.companies.c.name.label("company_name")) \
            .join(m.companies, m.company_sources.c.company_id == m.companies.c.id)
        rows = list(c.execute(rows_query.order_by(m.companies.c.name, m.company_sources.c.id)).mappings())
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        company_ids = {row["company_id"] for row in rows}
        domains = {}
        for domain in c.execute(select(m.company_domains).where(
                m.company_domains.c.company_id.in_(company_ids), m.company_domains.c.verified.is_(True))
                .order_by(m.company_domains.c.confidence.desc(), m.company_domains.c.domain)).mappings():
            domains.setdefault(domain["company_id"], []).append(dict(domain))
        posting_sources = m.postings.outerjoin(m.posting_value_candidates,
            m.posting_value_candidates.c.posting_id == m.postings.c.id)
        linked = m.company_sources.join(posting_sources, or_(
            m.postings.c.source == ("company-feed:" + m.company_sources.c.id),
            m.posting_value_candidates.c.company_source_id == m.company_sources.c.id)).join(m.jobs,
            (m.jobs.c.id == m.postings.c.job_id) & (m.jobs.c.company_id == m.company_sources.c.company_id))
        count_query = select(m.company_sources.c.id, func.count(func.distinct(m.jobs.c.id))).select_from(linked).group_by(m.company_sources.c.id)
        found = dict(c.execute(count_query).all())
        applications_query = select(
            m.jobs.c.company_id,
            func.count(func.distinct(m.applications.c.id)),
        ).select_from(m.applications.join(m.jobs, m.jobs.c.id == m.applications.c.job_id)) \
            .where(m.jobs.c.company_id.in_(company_ids))
        if profile_id is not None:
            applications_query = applications_query.where(m.applications.c.profile_id == profile_id)
        applications = dict(c.execute(applications_query.group_by(m.jobs.c.company_id)).all())
        relevant = {}
        if profile:
            matched = linked.join(m.matches, (m.matches.c.job_id == m.jobs.c.id)
                & (m.matches.c.profile_id == profile_id) & (m.matches.c.profile_version == profile["version"]))
            relevant = dict(c.execute(select(m.company_sources.c.id, func.count(func.distinct(m.jobs.c.id)))
                .select_from(matched).group_by(m.company_sources.c.id)).all())
        source_key = m.audit.c.payload["company_source_id"].as_string()
        ranked = select(m.audit, source_key.label("source_id"), func.row_number().over(
            partition_by=source_key, order_by=(m.audit.c.timestamp.desc(), m.audit.c.id.desc())).label("rank"))
        ranked = ranked.where(source_key.in_(ids), m.audit.c.action.in_([
            "COMPANY_SOURCE_DISCOVERED", "COMPANY_FEED_IMPORT_COMPLETE", "COMPANY_SOURCE_REFRESHED",
            "COMPANY_SOURCE_REFRESH_FAILED", "COMPANY_SOURCE_MIGRATION_REVIEW", "COMPANY_SOURCE_MONITORING_REVIEWED"])).subquery()
        events = {}
        for event in c.execute(select(ranked).where(ranked.c.rank <= 20).order_by(
                ranked.c.source_id, ranked.c.rank)).mappings():
            events.setdefault(event["source_id"], []).append(_view_event(event))
        tenants = {row["id"]: dict(row) for row in c.execute(select(m.company_ats_tenants).where(
            m.company_ats_tenants.c.company_id.in_(company_ids))).mappings()}
        qualities = {row['id']: company_source_quality(c, row) for row in rows}
        from services.company_discovery.monitoring import _validate, _profiles, source_version, supported_source
        from services.company_discovery.public_client import PublicFetchError
        monitors = {row['source_id']: dict(row) for row in c.execute(select(m.company_source_monitoring).where(
            m.company_source_monitoring.c.source_id.in_(ids))).mappings()}
        scheduled_profiles = bool(_profiles(c))
        from services.company_discovery.ats_enrollment import collector_view
        collectors = {row['id']: collector_view(store, c, row) for row in rows}
        for source in rows:
            monitor = monitors.get(source['id'])
            if monitor:
                try:
                    _validate(c, source, monitor)
                except (ValueError, PublicFetchError) as failure:
                    monitor.update(status='BLOCKED', last_error=str(failure), next_due=None)
                if monitor['status'] == 'SCHEDULED' and monitor['operation_id']:
                    monitor['status'] = c.execute(select(m.operations.c.state).where(
                        m.operations.c.id == monitor['operation_id'])).scalar_one()
                if not scheduled_profiles or monitor['status'] in {'BLOCKED','REDISCOVERY_REQUIRED','QUEUED','RUNNING'}:
                    monitor['next_due'] = None
    result = []
    for row in rows:
        verified = domains.get(row["company_id"], [])
        # Equal-confidence distinct verified domains remain visible as ambiguity.
        domain = verified[0] if verified and (len(verified) == 1 or verified[0]["confidence"] > verified[1]["confidence"]) else None
        tenant = tenants.get(row["ats_tenant_id"], {})
        confidence = row["confidence"]
        valid_confidence = type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1
        review = not valid_confidence or confidence < 0.75
        result.append({
            "id": row["id"], "company_id": row["company_id"], "company_name": row["company_name"],
            "profile_id": profile_id,
            "profile_company_associated": profile_company_ids is None or row["company_id"] in profile_company_ids,
            "official_domain": domain["domain"] if domain else None,
            "career_url": _view_url(row["career_url"]), "provider": row["provider"], "tenant": row["tenant"],
            "collection_strategy": row["collection_strategy"], "confidence": confidence if valid_confidence else None,
            "priority": row["priority"], "enabled": row["enabled"], "origin": row["origin"],
            "discovered_automatically": row["discovered_automatically"],
            "status": "COMPANY_SOURCE_REVIEW_REQUIRED" if review else ("ENABLED" if row["enabled"] else "DISABLED"),
            "jobs_found": found.get(row["id"], 0), "relevant_jobs": relevant.get(row["id"], 0),
            "applications": applications.get(row["company_id"], 0),
            "last_checked": row["last_validation"], "last_success": row["last_success"],
            "next_check": monitors.get(row['id'], {}).get('next_due'),
            "collector": collectors[row['id']],
            "source_version": source_version(row), "monitoring_supported": supported_source(row),
            "monitoring": {key: value for key, value in monitors.get(row['id'], {}).items()
                if key in {'request_timeout','rate_limit','concurrency','jitter_seconds','revision','enabled','poll_interval','status','failure_count','last_attempt','next_due','last_error',
                           'operation_id','rediscovery_operation_id','authorization'}} or None,
            "quality": qualities[row['id']],
            "provenance": {
                "domain_method": domain["verification_method"] if domain else None,
                "domain_candidates_ambiguous": len(verified) > 1 and domain is None,
                "tenant_evidence": [item[:500] for item in tenant.get("evidence", [])[:20] if isinstance(item, str)],
                "events": events.get(row["id"], []),
                "source_linked_history_available": bool(events.get(row["id"])),
            },
        })
    return result
