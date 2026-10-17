"""Preserve source provenance while planning genuinely distinct application routes."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.dedupe.signals import canonical_application_url


FIRST_PARTY_PROVIDERS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "workable",
    "smartrecruiters",
    "jobvite",
    "icims",
    "successfactors",
    "oracle",
    "mock-ats",
}

TRUSTED_INTEGRATED_BOARD_PROVIDERS = {"remotive", "remoteok", "remote_ok", "wwr"}
APPLICATION_REDIRECT_CAPABILITY = "APPLICATION_REDIRECT"


def route_authority(route):
    explicit = str(route.get("authority") or "").upper()
    if explicit in {"VERIFIED_EMPLOYER", "VERIFIED_ATS", "TRUSTED_INTEGRATED_BOARD",
                    "DIRECT_EMPLOYER", "AGGREGATOR", "UNKNOWN"}:
        return explicit
    provider = str(route.get("provider") or "").casefold()
    if provider in FIRST_PARTY_PROVIDERS:
        # A provider label identifies a platform, not this employer's tenant.
        return "UNKNOWN"
    if provider in {"employer", "company", "direct-employer"}:
        return "UNKNOWN"
    return "AGGREGATOR"


def route_underlying_key(route):
    tenant = str(route.get("tenant") or "").strip().casefold()
    requisition = str(route.get("requisition") or "").strip().casefold()
    company = str(route.get("company_id") or "").strip().casefold()
    if company and requisition:
        return "employer:" + company + ":" + requisition
    if tenant and requisition:
        return "ats:" + tenant + ":" + requisition
    url = canonical_application_url(route.get("url") or route.get("application_url"))
    if url:
        return "url:" + url
    identifier = str(route.get("id") or "").strip()
    if identifier:
        return "route:" + identifier
    raise ValueError("APPLICATION_ROUTE_IDENTITY_UNAVAILABLE")


def distinct_route_options(routes):
    if not isinstance(routes, list):
        raise ValueError("APPLICATION_ROUTES_INVALID")
    groups = {}
    rank = {
        "UNKNOWN": 0,
        "AGGREGATOR": 10,
        "TRUSTED_INTEGRATED_BOARD": 15,
        "DIRECT_EMPLOYER": 20,
        "VERIFIED_ATS": 30,
        "VERIFIED_EMPLOYER": 40,
    }
    for route in routes:
        if not isinstance(route, dict) or not route.get("id"):
            raise ValueError("APPLICATION_ROUTE_INVALID")
        key = route_underlying_key(route)
        authority = route_authority(route)
        row = {**route, "underlying_key": key, "authority": authority}
        group = groups.setdefault(key, [])
        group.append(row)
    result = []
    for key, group in groups.items():
        preferred = max(group, key=lambda row: (
            rank[row["authority"]], bool(row.get("preferred_by_profile")), str(row.get("id"))))
        result.append({
            "underlying_key": key,
            "preferred_route": preferred,
            "aliases": sorted(group, key=lambda row: str(row.get("id"))),
        })
    return sorted(result, key=lambda row: row["underlying_key"])


def plan_application_routes(routes, mode="DEFAULT", route_id=None, preference="PREFER_VERIFIED"):
    mode = str(mode or "DEFAULT").upper()
    preference = str(preference or "PREFER_VERIFIED").upper()
    if preference not in {"PREFER_VERIFIED", "USER_CHOICE"}:
        raise ValueError("APPLICATION_ROUTE_PREFERENCE_INVALID")
    if mode == "DEFAULT" and preference == "USER_CHOICE":
        raise ValueError("APPLICATION_ROUTE_SELECTION_REQUIRED")
    options = distinct_route_options(routes)
    if not options:
        raise ValueError("APPLICATION_ROUTE_UNAVAILABLE")
    by_id = {
        str(route["id"]): option
        for option in options
        for route in option["aliases"]
    }
    if mode == "THIS_ROUTE":
        if not route_id or str(route_id) not in by_id:
            raise ValueError("APPLICATION_ROUTE_SELECTION_INVALID")
        option = by_id[str(route_id)]
        requested = next(route for route in option['aliases'] if str(route['id']) == str(route_id))
        selected = [{**option, 'preferred_route': requested}]
    elif mode == "EMPLOYER_SITE":
        candidates = [
            option for option in options
            if option["preferred_route"]["authority"] in {"VERIFIED_EMPLOYER", "VERIFIED_ATS", "DIRECT_EMPLOYER"}
        ]
        if not candidates:
            raise ValueError("EMPLOYER_APPLICATION_ROUTE_UNAVAILABLE")
        selected = [max(
            candidates,
            key=lambda option: (
                {"DIRECT_EMPLOYER": 20, "TRUSTED_INTEGRATED_BOARD": 15,
                 "VERIFIED_ATS": 30, "VERIFIED_EMPLOYER": 40}[
                    option["preferred_route"]["authority"]
                ],
                option["underlying_key"],
            ),
        )]
    elif mode == "ALL_DISTINCT":
        selected = options
    elif mode == "DEFAULT":
        selected = [max(
            options,
            key=lambda option: (
                {"UNKNOWN": 0, "AGGREGATOR": 10, "TRUSTED_INTEGRATED_BOARD": 15,
                 "DIRECT_EMPLOYER": 20, "VERIFIED_ATS": 30, "VERIFIED_EMPLOYER": 40}[
                    option["preferred_route"]["authority"]
                ],
                bool(option["preferred_route"].get("preferred_by_profile")),
                option["underlying_key"],
            ),
        )]
    else:
        raise ValueError("APPLICATION_ROUTE_MODE_INVALID")
    return {
        "mode": mode,
        "selected": selected,
        "distinct_count": len(options),
        "all_options": options,
    }


def _has_integrated_board_route_evidence(connection, route):
    provider = str(route.get("provider") or "").casefold()
    if provider not in TRUSTED_INTEGRATED_BOARD_PROVIDERS:
        return False
    tenant = str(route.get("tenant") or "").casefold()
    source_rows = connection.execute(select(m.job_sources.c.provider, m.job_sources.c.tenant,
                                             m.job_sources.c.capabilities).select_from(
        m.postings.join(m.company_sources, m.postings.c.source == m.company_sources.c.endpoint)
        .join(m.job_sources, m.company_sources.c.collector_source_id == m.job_sources.c.id)
    ).where(
        m.postings.c.job_id == route.get("job_id"),
        m.postings.c.application_url == route.get("url"),
        m.company_sources.c.company_id == route.get("company_id"),
        m.company_sources.c.provider == provider,
        m.company_sources.c.tenant == tenant,
        m.job_sources.c.provider == provider,
        m.job_sources.c.tenant == tenant,
    )).mappings().all()
    for source in source_rows:
        capabilities = source["capabilities"]
        if (isinstance(capabilities, dict)
                and isinstance(capabilities.get("capabilities"), list)
                and APPLICATION_REDIRECT_CAPABILITY in capabilities["capabilities"]):
            return True
    return False


def qualify_route_authority(connection, routes):
    """Annotate routes only from exact, persisted employer, ATS, or board evidence."""
    result = [dict(route) for route in routes]
    for route in result:
        route["authority"] = route_authority(route)
        canonical_company_id = connection.execute(select(m.jobs.c.company_id).where(
            m.jobs.c.id == route.get("job_id"),
        )).scalar_one_or_none()
        if canonical_company_id is None or canonical_company_id != route.get("company_id"):
            route["authority"] = "UNKNOWN"
            continue
        parsed = urlsplit(str(route.get("url") or ""))
        host = (parsed.hostname or "").casefold().rstrip(".")
        domains = connection.execute(select(m.company_domains.c.domain).where(
            m.company_domains.c.company_id == route["company_id"],
            m.company_domains.c.verified.is_(True),
        )).scalars().all()
        normalized_domains = [str(domain).casefold().rstrip(".") for domain in domains]
        if host and any(host == domain or host.endswith("." + domain) for domain in normalized_domains):
            route["authority"] = "VERIFIED_EMPLOYER"
            continue
        tenants = connection.execute(select(m.company_ats_tenants).where(
            m.company_ats_tenants.c.company_id == route["company_id"],
            m.company_ats_tenants.c.provider == route["provider"],
            m.company_ats_tenants.c.tenant_identifier == route["tenant"],
        ).order_by(m.company_ats_tenants.c.id)).mappings().all()
        if route['provider'] in {'greenhouse', 'lever'}:
            from services.dedupe.route_redirects import qualified_ats_tenant
            if qualified_ats_tenant(str(route.get('url') or ''),
                                    [dict(row) for row in tenants]) is None:
                continue
        if (route["provider"] in FIRST_PARTY_PROVIDERS and len(tenants) == 1
                and 0.75 <= float(tenants[0]["confidence"]) <= 1.0
                and isinstance(tenants[0]["evidence"], list)
                and any(isinstance(item, str) and item.strip() for item in tenants[0]["evidence"])):
            route["authority"] = "VERIFIED_ATS"
            continue
        if _has_integrated_board_route_evidence(connection, route):
            route["authority"] = "TRUSTED_INTEGRATED_BOARD"
    return result


def save_verified_redirect_destination(connection, source_route, resolution):
    """Persist one freshly verified redirect candidate without selecting it."""
    status = str(resolution.get("status") or "")
    if status == "RESOLVED_ATS_ROUTE":
        provider = str(resolution.get("provider") or "").casefold()
        tenant = str(resolution.get("tenant") or "").strip()
        expected_authority = "VERIFIED_ATS"
        if provider not in FIRST_PARTY_PROVIDERS or not tenant:
            raise ValueError("ROUTE_DESTINATION_NOT_VERIFIED")
    elif status == "RESOLVED_EMPLOYER_ROUTE":
        provider = "employer"
        tenant = ""
        expected_authority = "VERIFIED_EMPLOYER"
    else:
        raise ValueError("ROUTE_DESTINATION_NOT_VERIFIED")

    url = str(resolution.get("candidate_url") or "")
    parsed = urlsplit(url)
    if (len(url) > 2000 or parsed.scheme.casefold() != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("ROUTE_DESTINATION_NOT_VERIFIED")
    try:
        parsed.port
    except ValueError:
        raise ValueError("ROUTE_DESTINATION_NOT_VERIFIED") from None

    company_id = str(source_route["company_id"])
    job_id = str(source_route["job_id"])
    existing = connection.execute(select(m.routes).where(
        m.routes.c.job_id == job_id,
        m.routes.c.company_id == company_id,
        m.routes.c.provider == provider,
        m.routes.c.tenant == tenant,
        m.routes.c.url == url,
    )).mappings().first()
    if existing is not None:
        if qualify_route_authority(connection, [dict(existing)])[0]["authority"] != expected_authority:
            raise ValueError("ROUTE_DESTINATION_NOT_VERIFIED")
        return {"route_id": str(existing["id"]), "created": False}

    # The route table requires a requisition key. This opaque, stable key keeps
    # redirected routes distinct without claiming the aggregator ID is an ATS ID.
    requisition = "redirect-" + hashlib.sha256(
        f"{job_id}\n{url}".encode("utf-8")
    ).hexdigest()[:48]
    route = {
        "id": identity("verified-redirect-route", company_id, job_id, provider, tenant, url),
        "job_id": job_id,
        "company_id": company_id,
        "provider": provider,
        "tenant": tenant,
        "requisition": requisition,
        "url": url,
    }
    if qualify_route_authority(connection, [route])[0]["authority"] != expected_authority:
        raise ValueError("ROUTE_DESTINATION_NOT_VERIFIED")
    upsert(connection, m.routes, route)
    return {"route_id": route["id"], "created": True}


def direct_employer_route_discovery(routes, source_route_id):
    """Resolve a listing to an already-known, verified ATS sibling by exact job identity.

    This is a local evidence lookup, not an HTTP redirect fetch. It never rewrites the
    requested route; callers must preserve a user's explicit route choice.
    """
    if not isinstance(routes, list):
        raise ValueError("APPLICATION_ROUTES_INVALID")
    source = next((route for route in routes if str(route.get("id")) == str(source_route_id)), None)
    if source is None:
        raise ValueError("APPLICATION_ROUTE_SELECTION_INVALID")
    if route_authority(source) != "AGGREGATOR":
        return {"status": "NOT_AGGREGATOR", "source_route_id": str(source_route_id),
                "candidate_route_ids": []}
    source_key = route_underlying_key(source)
    candidates = sorted(
        str(route["id"]) for route in routes
        if str(route.get("id")) != str(source_route_id)
        and route_authority(route) == "VERIFIED_ATS"
        and route_underlying_key(route) == source_key
    )
    status = "ATS_CANDIDATE_FOUND" if len(candidates) == 1 else "AMBIGUOUS" if candidates else "UNRESOLVED"
    return {"status": status, "source_route_id": str(source_route_id),
            "candidate_route_ids": candidates}


def load_job_routes(store, job_id, profile_id=None):
    with store.engine.connect() as connection:
        routes = [
            dict(row)
            for row in connection.execute(
                select(m.routes).where(m.routes.c.job_id == job_id).order_by(m.routes.c.id)
            ).mappings()
        ]
        qualified = qualify_route_authority(connection, routes)
        preferred = set()
        if profile_id:
            company_ids = {route["company_id"] for route in qualified}
            if len(company_ids) == 1:
                from services.dedupe.route_trust import preferred_route_identities
                preferred = preferred_route_identities(store, profile_id, next(iter(company_ids)))
        for route in qualified:
            route["preferred_by_profile"] = (route["provider"], route["tenant"]) in preferred
        for route in qualified:
            if route["authority"] == "AGGREGATOR":
                route["direct_employer_resolution"] = direct_employer_route_discovery(
                    qualified, route["id"])
        return qualified


def persist_route_choices(connection, application_id, route_plan):
    selected = route_plan["selected"]
    for option in selected:
        route = option["preferred_route"]
        key = option["underlying_key"]
        identifier = identity("application-route-choice", application_id, key)
        upsert(connection, m.application_route_choices, {
            "id": identifier,
            "application_id": application_id,
            "route_id": route["id"],
            "underlying_key": key,
            "requested_mode": route_plan["mode"],
            "state": "PLANNED",
            "created_at": now(),
        })
    return [option["preferred_route"]["id"] for option in selected]


def list_route_choices(store, application_id):
    with store.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(m.application_route_choices).where(
                    m.application_route_choices.c.application_id == application_id
                ).order_by(m.application_route_choices.c.id)
            ).mappings()
        ]


def source_provenance_inventory(postings):
    return [
        {
            "posting_id": row.get("id"),
            "source": row.get("source"),
            "source_job_id": row.get("source_job_id"),
            "source_url": row.get("source_url"),
            "application_url": row.get("application_url"),
            "fetched_at": row.get("fetched_at"),
        }
        for row in postings
    ]
