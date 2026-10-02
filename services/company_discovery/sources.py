"""Durable company ATS tenant/source records and explicit company hierarchy."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import math
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, identity, now, upsert


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
                              last_validation=None, last_success=_UNSET):
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
        legacy = connection.execute(select(m.company_sources.c.id).where(
            m.company_sources.c.company_id == company_id,
            m.company_sources.c.provider == provider,
            m.company_sources.c.tenant == tenant,
            m.company_sources.c.career_url.is_(None),
        ).order_by(m.company_sources.c.id).limit(1)).first()
        source_id = legacy.id if legacy else identity(company_id, provider, tenant, career_url or endpoint)
        if last_success is _UNSET:
            last_success = connection.execute(select(m.company_sources.c.last_success).where(
                m.company_sources.c.id == source_id
            )).scalar_one_or_none()
        upsert(connection, m.company_sources, {
            "id": source_id, "company_id": company_id, "provider": provider,
            "tenant": tenant, "endpoint": endpoint,
            "discovered_automatically": discovered_automatically,
            "ats_tenant_id": ats_tenant_id, "career_url": career_url,
            "search_url": search_url, "feed_url": feed_url, "public_api_url": public_api_url,
            "collection_strategy": strategy, "confidence": confidence, "enabled": enabled,
            "priority": priority, "last_validation": last_validation, "last_success": last_success,
        }, replace=True)
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
