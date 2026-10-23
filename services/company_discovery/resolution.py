"""Evidence-based company identity resolution without network side effects."""
from __future__ import annotations

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity
from services.company_discovery.aliases import normalize_alias
from services.company_discovery.domains import VERIFIED_METHODS, is_aggregator_domain, normalize_domain
from services.normalization.jobs import normalize_company_name


def _hostname(value):
    return normalize_domain(value)


def _authorized_ats_source(connection, source, tenants, domains):
    """Read current enrollment authority, not just a discovered tenant string."""
    tenant = tenants.get(source.get("ats_tenant_id"))
    if (not source["enabled"] or not source["last_validation"]
            or not source["confidence"] >= 0.90 or tenant is None
            or tenant["company_id"] != source["company_id"]
            or tenant["provider"] != source["provider"]
            or tenant["tenant_identifier"] != source["tenant"]
            or not tenant["confidence"] >= 0.90 or not tenant["evidence"]):
        return False
    # A reviewed source keeps its live request/audit/operation revocation gates.
    # Never fall back to a domain match after its explicit authorization fails.
    if source["identity_authorization"] is not None:
        from services.company_discovery.requests import authorize_confirmed_source
        try:
            resolved = authorize_confirmed_source(connection, source)
        except (ValueError, KeyError, TypeError):
            return False
        return (resolved["company_id"] == source["company_id"]
                and "USER_CONFIRMED_DOMAIN" in resolved["evidence"])
    host = _hostname(source["career_url"])
    if not host or is_aggregator_domain(host):
        return False
    # Match the enrollment contract's exact official career host. Conflicting
    # verified owners are not authority for either employer, even at equal scores.
    owners = {row["company_id"] for row in domains
              if row["domain"] == host and row["verification_method"] in VERIFIED_METHODS
              and row["evidence"]}
    return owners == {source["company_id"]}


def resolve_company(store, candidate):
    """Return stable identity using verified evidence; candidate URLs remain unverified."""
    if not isinstance(candidate, dict):
        raise ValueError("COMPANY_CANDIDATE_INVALID")
    name = str(candidate.get("company_name") or candidate.get("name") or "").strip()
    if not name:
        raise ValueError("COMPANY_NAME_REQUIRED")
    website = str(candidate.get("company_website") or candidate.get("website_url") or "").strip()
    application_url = str(candidate.get("application_url") or "").strip()
    ats_tenant = str(candidate.get("ats_tenant") or candidate.get("tenant") or "").strip().casefold()
    website_host = _hostname(website)
    application_host = _hostname(application_url)
    normalized_name = normalize_company_name(name)
    with store.engine.connect() as connection:
        companies = [dict(row) for row in connection.execute(select(m.companies)).mappings()]
        sources = [dict(row) for row in connection.execute(select(m.company_sources)).mappings()]
        domains = [dict(row) for row in connection.execute(select(m.company_domains).where(
            m.company_domains.c.verified.is_(True)
        )).mappings()]
        aliases = [dict(row) for row in connection.execute(select(m.company_aliases).where(
            m.company_aliases.c.verified.is_(True)
        )).mappings()]
        matching_sources = [source for source in sources if ats_tenant
                            and str(source["tenant"] or "").casefold() == ats_tenant]
        if matching_sources:
            tenants = {row["id"]: dict(row) for row in connection.execute(
                select(m.company_ats_tenants)).mappings()}
            sources = [source for source in matching_sources
                       if _authorized_ats_source(connection, source, tenants, domains)]
        else:
            sources = []
    by_id = {company["id"]: company for company in companies}
    source_by_company = {}
    for source in sources:
        source_by_company.setdefault(source["company_id"], []).append(source)

    scored = {}

    def add(company_id, confidence, signal, domain=""):
        company = by_id.get(company_id)
        if not company:
            return
        current = scored.setdefault(company_id, {"confidence": 0.0, "evidence": [], "domain": ""})
        current["confidence"] = max(current["confidence"], confidence)
        if signal not in current["evidence"]:
            current["evidence"].append(signal)
        if domain:
            current["domain"] = domain

    if website_host and not is_aggregator_domain(website_host):
        for domain in domains:
            host = domain["domain"]
            if website_host == host or website_host.endswith("." + host):
                add(domain["company_id"], float(domain["confidence"]), "VERIFIED_DOMAIN_MATCH", host)
    if application_host and not is_aggregator_domain(application_host):
        for domain in domains:
            host = domain["domain"]
            if application_host == host or application_host.endswith("." + host):
                add(domain["company_id"], min(float(domain["confidence"]), 0.90),
                    "VERIFIED_DOMAIN_MATCH", host)

    for alias in aliases:
        if alias["normalized_alias"] == normalize_alias(name):
            add(alias["company_id"], float(alias["confidence"]), "VERIFIED_ALIAS_EXACT")
    for company in companies:
        if normalized_name and normalize_company_name(company.get("name")) == normalized_name:
            add(company["id"], 0.75, "NORMALIZED_NAME_EXACT")
        if ats_tenant and any(str(row.get("tenant") or "").casefold() == ats_tenant
                              for row in source_by_company.get(company["id"], [])):
            add(company["id"], 0.95, "ATS_TENANT_MATCH")

    if scored:
        highest = max(item["confidence"] for item in scored.values())
        winners = [company_id for company_id, item in scored.items() if item["confidence"] == highest]
        if len(winners) == 1:
            company_id = winners[0]
            company = by_id[company_id]
            result = scored[company_id]
            return {"company_id": company_id, "confidence": result["confidence"],
                    "evidence": result["evidence"], "is_new": False,
                    "name": company["name"], "domain": result["domain"]}

    company_id = identity(website) if website else identity("company-name", normalized_name)
    untrusted_website = bool(website_host and is_aggregator_domain(website_host))
    candidate_domain = website_host if website_host and not is_aggregator_domain(website_host) else ""
    if len(scored) > 1:
        confidence, evidence = 0.45, ["AMBIGUOUS_COMPANY_MATCH", "CANDIDATE_DOMAIN_UNVERIFIED"]
    elif candidate_domain:
        confidence, evidence = 0.45, ["CANDIDATE_DOMAIN_UNVERIFIED"]
    elif untrusted_website:
        confidence, evidence = 0.45, ["UNTRUSTED_AGGREGATOR_DOMAIN"]
    else:
        confidence, evidence = 0.55, ["NORMALIZED_NAME_ONLY"]
    return {"company_id": company_id, "confidence": confidence, "evidence": evidence,
            "is_new": True, "name": name, "domain": candidate_domain}
