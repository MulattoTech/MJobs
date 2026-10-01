"""Bounded company-career discovery and immediate first-party enrollment."""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.company_discovery.resolution import resolve_company


def discover_company_source(store, candidate, client, check=None):
    """Resolve one employer, prove its careers/ATS source, enroll, and enumerate immediately."""
    check = check or (lambda: None)
    resolved = resolve_company(store, candidate)
    website = str(candidate.get("company_website") or candidate.get("website_url") or "").strip()
    if not website:
        raise ValueError("COMPANY_OFFICIAL_WEBSITE_REQUIRED")
    check()
    homepage = client.get(website)
    careers_link = BeautifulSoup(homepage.text, "html.parser").find("a", attrs={"rel": "careers"})
    if careers_link is None or not careers_link.get("href"):
        raise ValueError("COMPANY_CAREERS_LINK_UNPROVEN")
    careers_url = urljoin(str(homepage.url), careers_link["href"])
    check()
    careers = client.get(careers_url)
    # PR-016 proves the extensible discovery boundary with the fixture detector.
    # Production ATS families remain independently tracked provider requirements.
    ats_link = BeautifulSoup(careers.text, "html.parser").find("a", attrs={"data-provider": True, "data-tenant": True})
    if ats_link is None or not ats_link.get("href"):
        raise ValueError("COMPANY_ATS_REVIEW_REQUIRED")
    provider = str(ats_link["data-provider"]).strip().casefold()
    tenant = str(ats_link["data-tenant"]).strip().casefold()
    if provider != "mock-ats":
        raise ValueError("COMPANY_ATS_UNSUPPORTED")
    endpoint = urljoin(str(careers.url), ats_link["href"])
    check()
    payload = client.get(endpoint).json()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("COMPANY_JOB_ENUMERATION_INVALID")
    source_id = identity(resolved["company_id"], provider, tenant)
    with store.engine.begin() as connection:
        upsert(connection, m.companies, {
            "id": resolved["company_id"],
            "name": resolved["name"],
            "domain": website,
        }, replace=True)
        upsert(connection, m.company_sources, {
            "id": source_id,
            "company_id": resolved["company_id"],
            "provider": provider,
            "tenant": tenant,
            "endpoint": endpoint,
            "discovered_automatically": True,
        }, replace=True)
        store._audit(connection, resolved["company_id"], "COMPANY_SOURCE_DISCOVERED", {
            "trigger_evidence": candidate.get("trigger_evidence", []),
            "resolution_confidence": resolved["confidence"],
            "resolution_evidence": resolved["evidence"],
            "careers_url": careers_url,
            "provider": provider,
            "tenant": tenant,
            "endpoint": endpoint,
            "jobs_enumerated": len(jobs),
        })
    return {
        "company": resolved,
        "company_source_id": source_id,
        "provider": provider,
        "tenant": tenant,
        "careers_url": careers_url,
        "endpoint": endpoint,
        "jobs": jobs,
        "jobs_enumerated": len(jobs),
    }


def directly_monitored(store, company_id):
    with store.engine.connect() as connection:
        return connection.execute(select(m.company_sources.c.id).where(
            m.company_sources.c.company_id == company_id
        ).limit(1)).first() is not None
