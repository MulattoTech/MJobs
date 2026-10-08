"""Bounded company-career discovery and immediate first-party enrollment."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import now, upsert
from services.company_discovery.career_pages import (
    common_career_routes, internal_career_links, parse_sitemap, sitemap_locations,
)
from services.company_discovery.domains import normalize_domain, record_company_domain
from services.company_discovery.endpoints import machine_readable_job_endpoints, parse_public_job_feed
from services.company_discovery.fetch_policy import bounded_get
from services.company_discovery.quality import record_known_feed_failure
from services.company_discovery.resolution import resolve_company
from services.company_discovery.public_resolution import resolve_public_company
from services.company_discovery.public_client import PublicFetchError, HTTPStatusError as PublicHTTPStatusError
from services.company_discovery.search import search_company_careers
from services.company_discovery.sources import register_ats_tenant, upsert_company_job_source
from services.ats_detection.detector import detect_ats


_LEGAL_NAME_WORDS = {"inc", "incorporated", "corp", "corporation", "llc", "ltd", "limited", "company", "co"}
_MAX_SITEMAP_FETCHES = 5
_MAX_CHILD_SITEMAPS = 3
_MAX_SITEMAP_CAREER_PAGES = 10


def _same_origin(left, right):
    """Return whether two HTTP URLs share an origin, including default-port normalization."""
    try:
        def origin(value):
            parsed = urlsplit(str(value or ""))
            if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                return None
            scheme = parsed.scheme.casefold()
            port = parsed.port or (443 if scheme == "https" else 80)
            return scheme, parsed.hostname.casefold().rstrip("."), port
        return origin(left) is not None and origin(left) == origin(right)
    except ValueError:
        return False


def _sitemap_career_candidates(homepage, client, check):
    """Read a bounded same-origin sitemap chain and return career URL candidates."""
    homepage_url = str(homepage.url)
    sitemap_queue = []
    for location in sitemap_locations(homepage_url, homepage.text)[:_MAX_SITEMAP_FETCHES]:
        if _same_origin(homepage_url, location) and location not in sitemap_queue:
            sitemap_queue.append(location)
    candidates = []
    child_count = 0
    fetched = 0
    while sitemap_queue and fetched < _MAX_SITEMAP_FETCHES:
        sitemap_url = sitemap_queue.pop(0)
        if not _same_origin(homepage_url, sitemap_url):
            continue
        check()
        try:
            response = bounded_get(client, sitemap_url, homepage_url)
            fetched += 1
            parsed = parse_sitemap(str(response.url), response.text)
        except (httpx.HTTPError, ValueError, PublicFetchError):
            # A missing, malformed, denied, or stale sitemap should leave the existing
            # career-link and common-route discovery paths available.
            fetched += 1
            continue
        if not isinstance(parsed, dict):
            continue
        for career_url in parsed.get("career_pages", ()):
            if _same_origin(homepage_url, career_url) and career_url not in candidates:
                candidates.append(career_url)
                if len(candidates) >= _MAX_SITEMAP_CAREER_PAGES:
                    break
        for child_url in parsed.get("child_sitemaps", ()):
            if (child_count >= _MAX_CHILD_SITEMAPS or fetched + len(sitemap_queue) >= _MAX_SITEMAP_FETCHES):
                break
            if _same_origin(homepage_url, child_url) and child_url not in sitemap_queue:
                sitemap_queue.append(child_url)
                child_count += 1
    return candidates


def _career_page_is_corroborated(page, fixture):
    html = page.text
    fixture_ats = BeautifulSoup(html, "html.parser").find(
        "a", attrs={"data-provider": True, "data-tenant": True}
    )
    detected = detect_ats(
        str(page.url), html,
        redirect_chain=[str(response.url) for response in getattr(page, "history", ())],
    )
    return ((fixture and fixture_ats is not None) or detected.status == "MATCH"
            or bool(machine_readable_job_endpoints(str(page.url), html)))


def _company_name_appears(company_name, html):
    expected = {word for word in re.findall(r"[a-z0-9]+", str(company_name or "").casefold())
                if len(word) > 1 and word not in _LEGAL_NAME_WORDS}
    if not expected:
        return False
    visible = BeautifulSoup(str(html or "")[:2_000_000], "html.parser").get_text(" ", strip=True).casefold()
    observed = set(re.findall(r"[a-z0-9]+", visible))
    return expected.issubset(observed)


def _search_provenance(candidate, provider, selected, rejected, attempted=False):
    company_name = str(candidate.get("company_name") or "").strip()
    query = f"{' '.join(company_name.split())[:200]} careers jobs" if company_name else ""
    return {
        "discovery_method": "WEB_SEARCH" if attempted else "PROVIDED_COMPANY_WEBSITE",
        "search_provider": (selected.provider if selected else str(getattr(provider, "provider_id", "disabled")))
        if attempted else None,
        "search_query": (selected.query if selected else query) if attempted else None,
        "search_result_url": selected.url if selected else None,
        "search_rejected_candidates": list(rejected),
    }


def discover_company_source(store, candidate, client, check=None, search_provider=None,
                            profile_id=None, profile_version=None, preserve_source_id=None, enrollment_guard=None):
    """Resolve one employer, prove its careers/ATS source, enroll, and enumerate immediately."""
    check = check or (lambda: None)
    fixture = bool(getattr(client, "fixture_only", False))
    resolved = resolve_company(store, candidate) if fixture else resolve_public_company(store, candidate)
    website = str(candidate.get("company_website") or candidate.get("website_url") or "").strip()
    search_context = None
    rejected_search_candidates = []
    homepage = None
    search_attempted = False
    if not website:
        search_attempted = True
        search_candidates = search_company_careers(candidate.get("company_name"), search_provider)
        if not search_candidates:
            provenance = _search_provenance(candidate, search_provider, None, (), attempted=True)
            with store.engine.begin() as connection:
                store._audit(connection, resolved["company_id"], "COMPANY_SEARCH_REVIEW", {
                    "status": "NO_SEARCH_CANDIDATES",
                    "candidate_count": 0,
                    **provenance,
                })
            raise ValueError("COMPANY_OFFICIAL_WEBSITE_REQUIRED")
        for search_candidate in search_candidates:
            check()
            try:
                candidate_page = bounded_get(client, search_candidate.url)
            except (httpx.HTTPStatusError, PublicHTTPStatusError) as error:
                if isinstance(error, PublicHTTPStatusError) and error.status_code in {401, 403, 429}:
                    raise
                rejected_search_candidates.append({"url": search_candidate.url, "reason": "HTTP_STATUS"})
                continue
            except ValueError as error:
                if str(error) in {"COMPANY_DISCOVERY_URL_BLOCKED", "COMPANY_DISCOVERY_REDIRECT_BLOCKED"}:
                    rejected_search_candidates.append({"url": search_candidate.url, "reason": "FETCH_POLICY"})
                    continue
                raise
            fixture_ats = BeautifulSoup(candidate_page.text, "html.parser").find(
                "a", attrs={"data-provider": True, "data-tenant": True}
            )
            candidate_detection = detect_ats(
                str(candidate_page.url), candidate_page.text,
                redirect_chain=[str(response.url) for response in getattr(candidate_page, "history", ())],
            )
            if not (internal_career_links(str(candidate_page.url), candidate_page.text)
                    or (fixture and fixture_ats is not None) or candidate_detection.status == "MATCH"):
                rejected_search_candidates.append({"url": search_candidate.url,
                                                   "reason": "CAREER_OR_ATS_EVIDENCE_MISSING"})
                continue
            if not _company_name_appears(candidate.get("company_name"), candidate_page.text):
                rejected_search_candidates.append({"url": search_candidate.url,
                                                   "reason": "COMPANY_NAME_NOT_FOUND"})
                continue
            website = search_candidate.url
            homepage = candidate_page
            search_context = search_candidate
            break
        if not website:
            provenance = _search_provenance(candidate, search_provider, None, rejected_search_candidates,
                                            attempted=search_attempted)
            with store.engine.begin() as connection:
                store._audit(connection, resolved["company_id"], "COMPANY_SEARCH_REVIEW", {
                    "status": "NO_RELEVANT_COMPANY_SITE",
                    "candidate_count": len(search_candidates),
                    **provenance,
                })
            raise ValueError("COMPANY_OFFICIAL_WEBSITE_NOT_FOUND")
        candidate = {**candidate, "company_website": website}
    check()
    if homepage is None:
        homepage = bounded_get(client, website)
    careers_links = internal_career_links(str(homepage.url), homepage.text)
    careers = None
    careers_url = careers_links[0] if careers_links else ""
    if careers_url:
        check()
        try:
            careers = bounded_get(client, careers_url, str(homepage.url))
        except (httpx.HTTPStatusError, PublicHTTPStatusError) as error:
            status = error.status_code if isinstance(error, PublicHTTPStatusError) else error.response.status_code
            if status not in {404, 410}:
                raise
            careers_url = ""
    # A linked careers page remains authoritative when its content exposes an ATS or
    # supported public feed. If it is absent or too thin to identify either, inspect
    # a small same-origin sitemap chain before trying low-confidence common routes.
    if careers is None or not _career_page_is_corroborated(careers, fixture):
        sitemap_candidates = _sitemap_career_candidates(homepage, client, check)
        for sitemap_candidate in sitemap_candidates:
            check()
            try:
                candidate_page = bounded_get(client, sitemap_candidate, str(homepage.url))
            except (httpx.HTTPError, ValueError, PublicFetchError):
                continue
            if _career_page_is_corroborated(candidate_page, fixture):
                careers_url, careers = str(candidate_page.url), candidate_page
                break
    if careers is None or not _career_page_is_corroborated(careers, fixture):
        # Guesses are low-confidence probes only. A guessed route is accepted only
        # after its fetched content exposes a supported ATS integration.
        for guessed_url in common_career_routes(str(homepage.url)):
            check()
            try:
                guessed_page = bounded_get(client, guessed_url, str(homepage.url))
            except (httpx.HTTPStatusError, PublicHTTPStatusError) as error:
                status = error.status_code if isinstance(error, PublicHTTPStatusError) else error.response.status_code
                if status == 404:
                    continue
                raise
            if _career_page_is_corroborated(guessed_page, fixture):
                careers_url, careers = str(guessed_page.url), guessed_page
                break
    if careers is None:
        provenance = _search_provenance(candidate, search_provider, search_context, rejected_search_candidates,
                                        attempted=search_attempted)
        with store.engine.begin() as connection:
            store._audit(connection, resolved["company_id"], "COMPANY_CAREERS_DISCOVERY_REVIEW", {
                "status": "CAREERS_LINK_UNPROVEN",
                "homepage_url": str(homepage.url),
                **provenance,
            })
        raise (ValueError if fixture else PublicFetchError)("COMPANY_CAREERS_LINK_UNPROVEN")
    check()
    ats_link = BeautifulSoup(careers.text, "html.parser").find("a", attrs={"data-provider": True, "data-tenant": True})
    if not fixture and ats_link is not None and str(ats_link.get("data-provider", "")).strip().casefold() == "mock-ats":
        raise PublicFetchError("FIXTURE_SOURCE_BLOCKED")
    if fixture and ats_link is not None and ats_link.get("href") and str(ats_link.get("data-provider", "")).strip().casefold() == "mock-ats":
        provider = "mock-ats"
        tenant = str(ats_link["data-tenant"]).strip().casefold()
        endpoint = urljoin(str(careers.url), ats_link["href"])
    else:
        redirects = [str(response.url) for response in getattr(careers, "history", ())]
        detected = detect_ats(str(careers.url), careers.text, redirect_chain=redirects)
        with store.engine.begin() as connection:
            store._audit(connection, resolved["company_id"], "COMPANY_ATS_DETECTION_REVIEW", {
                "status": detected.status,
                "provider": detected.provider,
                "confidence": detected.confidence,
                "evidence": list(detected.evidence),
                "candidates": [
                    {"provider": item.provider, "confidence": item.confidence,
                     "evidence": list(item.evidence)} for item in detected.candidates
                ],
                "visible_careers_url": str(careers.url),
                "application_supported": detected.application_supported,
                **_search_provenance(candidate, search_provider, search_context,
                                     rejected_search_candidates, attempted=search_attempted),
            })
        feeds = (machine_readable_job_endpoints(str(careers.url), careers.text)
                 if detected.status != "MATCH" or not detected.provider else [])
        for feed in feeds:
            check()
            feed_available = None
            try:
                feed_response = bounded_get(client, feed["url"], str(careers.url))
                feed_available = True
                feed_jobs = parse_public_job_feed(feed_response.text, feed["strategy"])
            except (httpx.HTTPError, ValueError, PublicFetchError) as failure:
                record_known_feed_failure(store, company_id=resolved['company_id'],
                    career_url=str(careers.url), endpoint=feed['url'], strategy=feed['strategy'],
                    available=False if isinstance(failure, (httpx.HTTPError, PublicHTTPStatusError)) else feed_available,
                    error_code=type(failure).__name__)
                if isinstance(failure, PublicFetchError) and not (
                        isinstance(failure, PublicHTTPStatusError) and failure.status_code in {404, 410}):
                    raise
                continue
            company_domain = normalize_domain(str(homepage.url)) or ""
            source_provider = "rss" if feed["strategy"] == "RSS" else "json_feed"
            source_tenant = resolved["company_id"]
            if preserve_source_id:
                from services.company_discovery.sources import _url
                with store.engine.begin() as connection:
                    previous = connection.execute(select(m.company_sources).where(
                        m.company_sources.c.id == preserve_source_id)).mappings().one()
                    changed = (previous['company_id'] != resolved['company_id'] or previous['provider'] != source_provider
                        or previous['tenant'] != source_tenant or previous['endpoint'] != _url(feed['url'], 'ENDPOINT')
                        or previous['career_url'] != _url(str(careers.url), 'CAREER_URL'))
                    if changed:
                        store._audit(connection, previous['id'], 'COMPANY_SOURCE_MIGRATION_REVIEW',
                            dict(company_source_id=previous['id'], provider=source_provider, tenant=source_tenant,
                                 endpoint=feed['url'], careers_url=str(careers.url)))
                if changed:
                    raise PublicFetchError('COMPANY_SOURCE_MIGRATION_REVIEW_REQUIRED')
            timestamp = now()
            tenant_record = None
            if fixture:
                with store.engine.begin() as connection:
                    upsert(connection, m.companies, {
                        "id": resolved["company_id"], "name": resolved["name"], "domain": "",
                    }, replace=True)
                tenant_record = register_ats_tenant(
                    store, resolved["company_id"], provider=source_provider,
                    tenant_identifier=source_tenant, confidence=0.88,
                    evidence=["same-origin alternate machine-readable job feed"],
                )
            check()
            with store.engine.begin() as connection:
                if not fixture:
                    if connection.dialect.name == "sqlite":
                        connection.exec_driver_sql("BEGIN IMMEDIATE")
                    if enrollment_guard:
                        enrollment_guard(connection)
                    fresh = resolve_public_company(store, candidate, connection=connection, lock_verified=True)
                    if fresh["company_id"] != resolved["company_id"]:
                        raise PublicFetchError("COMPANY_IDENTITY_CHANGED")
                    resolved = fresh
                    upsert(connection, m.companies, {
                        "id": resolved["company_id"], "name": resolved["name"], "domain": "",
                    }, replace=True)
                verified_origin = "VERIFIED_DOMAIN_MATCH" in resolved["evidence"]
                source_confidence = 0.88 if fixture or verified_origin else 0.45
                source_record = upsert_company_job_source(
                    connection, company_id=resolved["company_id"], provider=source_provider,
                    tenant_identifier=source_tenant, career_url=str(careers.url),
                    feed_url=feed["url"] if feed["strategy"] == "RSS" else None,
                    public_api_url=feed["url"] if feed["strategy"] == "PUBLIC_JSON" else None,
                    collection_strategy=feed["strategy"], confidence=source_confidence, enabled=fixture or verified_origin,
                    priority="NORMAL", discovered_automatically=True,
                    ats_tenant_id=tenant_record["id"] if tenant_record else None, last_validation=timestamp,
                    profile_id=profile_id,
                )
                if preserve_source_id and source_record['id'] != preserve_source_id:
                    raise PublicFetchError('COMPANY_SOURCE_MIGRATION_REVIEW_REQUIRED')
            if company_domain:
                record_company_domain(
                    store, resolved["company_id"], company_domain,
                    "CANDIDATE_DOMAIN_UNVERIFIED",
                    ["homepage supplied for bounded career discovery"],
                )
            from services.discovery.company_feed import import_company_feed_jobs
            from services.company_discovery.application_links import sample_feed_links
            try:
                link_reports = None if fixture else sample_feed_links(
                    feed_jobs, feed["strategy"], feed["url"], client, check=check)
            except (ValueError, TypeError) as failure:
                # A malformed fetched feed is a failed import, not missing history.
                # Operational cancellation is deliberately not caught here.
                from services.company_discovery.quality import record_observation
                with store.engine.begin() as connection:
                    record_observation(connection, company_source_id=source_record['id'],
                        source_endpoint=feed['url'], succeeded=False, available=True,
                        enumerated=len(feed_jobs), error_code=type(failure).__name__)
                raise
            jobs_imported = import_company_feed_jobs(
                store, company_id=resolved["company_id"],
                source_id=source_record["id"], provider=source_provider, tenant=source_tenant,
                endpoint=feed["url"], strategy=feed["strategy"], items=feed_jobs,
                profile_id=profile_id, profile_version=profile_version, check=check,
                fetch_available=True, link_reports=link_reports,
                import_guard=enrollment_guard,
            )
            with store.engine.begin() as connection:
                store._audit(connection, resolved["company_id"], "COMPANY_SOURCE_DISCOVERED", {
                    "company_source_id": source_record["id"],
                    "discovery_method": "SAME_ORIGIN_MACHINE_READABLE_FEED",
                    "careers_url": str(careers.url), "endpoint": feed["url"],
                    "collection_strategy": feed["strategy"], "jobs_enumerated": len(feed_jobs),
                    "jobs_imported": jobs_imported,
                })
            return {
                "company": resolved, "company_source_id": source_record["id"],
                "provider": source_provider, "tenant": source_tenant,
                "careers_url": str(careers.url), "endpoint": feed["url"],
                "jobs": feed_jobs, "jobs_enumerated": len(feed_jobs), "jobs_imported": jobs_imported,
                "company_domain": company_domain,
            }
        if detected.status != "MATCH" or not detected.provider:
            raise (ValueError if fixture else PublicFetchError)("COMPANY_ATS_REVIEW_REQUIRED")
        raise (ValueError if fixture else PublicFetchError)("COMPANY_ATS_ENUMERATION_UNSUPPORTED")
    check()
    payload = bounded_get(client, endpoint, str(careers.url)).json()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("COMPANY_JOB_ENUMERATION_INVALID")
    with store.engine.begin() as connection:
        upsert(connection, m.companies, {
            "id": resolved["company_id"],
            "name": resolved["name"],
            # Keep observations out of the legacy field that has no verification semantics.
            "domain": "",
        }, replace=True)
    timestamp = now()
    tenant_record = register_ats_tenant(
        store, resolved["company_id"], provider=provider, tenant_identifier=tenant,
        confidence=0.95, evidence=["fixture ATS tenant metadata"],
    )
    source_record = upsert_company_job_source(
        store, company_id=resolved["company_id"], provider=provider, tenant_identifier=tenant,
        career_url=careers_url or str(careers.url), public_api_url=endpoint,
        collection_strategy="FIXTURE", confidence=0.95, enabled=True, priority="NORMAL",
        discovered_automatically=True, ats_tenant_id=tenant_record["id"],
        last_validation=timestamp, last_success=timestamp, profile_id=profile_id,
    )
    source_id = source_record["id"]
    with store.engine.begin() as connection:
        store._audit(connection, resolved["company_id"], "COMPANY_SOURCE_DISCOVERED", {
            "company_source_id": source_id,
            "trigger_evidence": candidate.get("trigger_evidence", []),
            "resolution_confidence": resolved["confidence"],
            "resolution_evidence": resolved["evidence"],
            "careers_url": careers_url,
            "provider": provider,
            "tenant": tenant,
            "endpoint": endpoint,
            "jobs_enumerated": len(jobs),
            **_search_provenance(candidate, search_provider, search_context, rejected_search_candidates,
                                 attempted=search_attempted),
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


def directly_monitored(store, company_id, *, public=False):
    with store.engine.connect() as connection:
        query = select(m.company_sources).where(m.company_sources.c.company_id == company_id)
        if public:
            query = query.where(m.company_sources.c.enabled.is_(True),
                                m.company_sources.c.collection_strategy != "FIXTURE",
                                m.company_sources.c.last_success.is_not(None))
            from services.company_discovery.monitoring import _monitor, _validate, _profiles
            profiles = list(connection.execute(select(m.search_profiles).where(m.search_profiles.c.enabled.is_(True))).mappings())
            for source in connection.execute(query).mappings():
                if source['collector_source_id']:
                    collector = connection.execute(select(m.job_sources).where(m.job_sources.c.id == source['collector_source_id'])).mappings().one()
                    if collector['enabled'] and any(not row['source_ids'] or collector['id'] in row['source_ids'] for row in profiles):
                        return True
                    continue
                monitor = _monitor(connection, source['id'])
                if not monitor or monitor['status'] in {'BLOCKED','REDISCOVERY_REQUIRED'} or not _profiles(connection):
                    continue
                try:
                    _validate(connection, source, monitor)
                    return True
                except (ValueError, PublicFetchError):
                    continue
            return False
        return connection.execute(query.limit(1)).first() is not None
