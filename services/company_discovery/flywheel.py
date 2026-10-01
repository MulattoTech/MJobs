"""Bounded company-career discovery and immediate first-party enrollment."""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup
import httpx
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, upsert
from services.company_discovery.career_pages import common_career_routes, internal_career_links
from services.company_discovery.domains import normalize_domain, record_company_domain
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
    careers_links = internal_career_links(str(homepage.url), homepage.text)
    careers = None
    careers_url = careers_links[0] if careers_links else ""
    if careers_url:
        check()
        careers = client.get(careers_url)
    else:
        # Guesses are low-confidence probes only. A guessed route is accepted only
        # after its fetched content exposes a supported ATS integration.
        for guessed_url in common_career_routes(str(homepage.url)):
            check()
            try:
                guessed_page = client.get(guessed_url)
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    continue
                raise
            guessed_ats = BeautifulSoup(guessed_page.text, "html.parser").find(
                "a", attrs={"data-provider": True, "data-tenant": True}
            )
            if guessed_ats is not None:
                careers_url, careers = str(guessed_page.url), guessed_page
                break
    if careers is None:
        raise ValueError("COMPANY_CAREERS_LINK_UNPROVEN")
    check()
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
            # Legacy domain storage has no verification semantics. Keep it empty;
            # all observed domains go through the evidence-aware relation below.
            "domain": "",
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
    homepage_domain = normalize_domain(str(homepage.url))
    if homepage_domain:
        record_company_domain(store, resolved["company_id"], homepage_domain,
                              "CANDIDATE_DOMAIN_UNVERIFIED",
                              ["homepage supplied for bounded career discovery"])
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
