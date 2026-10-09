"""Preserve source provenance while planning genuinely distinct application routes."""
from __future__ import annotations

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


def route_authority(route):
    explicit = str(route.get("authority") or "").upper()
    if explicit in {"VERIFIED_EMPLOYER", "VERIFIED_ATS", "DIRECT_EMPLOYER", "AGGREGATOR", "UNKNOWN"}:
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
        preferred = max(group, key=lambda row: (rank[row["authority"]], str(row.get("id"))))
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
                {"DIRECT_EMPLOYER": 20, "VERIFIED_ATS": 30, "VERIFIED_EMPLOYER": 40}[
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
                {"UNKNOWN": 0, "AGGREGATOR": 10, "DIRECT_EMPLOYER": 20, "VERIFIED_ATS": 30, "VERIFIED_EMPLOYER": 40}[
                    option["preferred_route"]["authority"]
                ],
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


def qualify_route_authority(connection, routes):
    """Annotate routes only from exact, persisted employer or ATS identity evidence."""
    result = [dict(route) for route in routes]
    for route in result:
        route["authority"] = route_authority(route)
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
        if (route["provider"] in FIRST_PARTY_PROVIDERS and len(tenants) == 1
                and 0.75 <= float(tenants[0]["confidence"]) <= 1.0
                and isinstance(tenants[0]["evidence"], list)
                and any(isinstance(item, str) and item.strip() for item in tenants[0]["evidence"])):
            route["authority"] = "VERIFIED_ATS"
    return result


def load_job_routes(store, job_id):
    with store.engine.connect() as connection:
        routes = [
            dict(row)
            for row in connection.execute(
                select(m.routes).where(m.routes.c.job_id == job_id).order_by(m.routes.c.id)
            ).mappings()
        ]
        return qualify_route_authority(connection, routes)


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
