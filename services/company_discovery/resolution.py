"""Evidence-based company identity resolution without network side effects."""
from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity
from services.normalization.jobs import normalize_company_name


def _hostname(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return parsed.hostname.casefold().removeprefix("www.")


def resolve_company(store, candidate):
    """Return stable company identity, confidence, and explainable evidence."""
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
    evidence = []
    with store.engine.connect() as connection:
        companies = [dict(row) for row in connection.execute(select(m.companies)).mappings()]
        sources = [dict(row) for row in connection.execute(select(m.company_sources)).mappings()]
    source_by_company = {}
    for source in sources:
        source_by_company.setdefault(source["company_id"], []).append(source)

    scored = []
    for company in companies:
        score = 0.0
        signals = []
        company_host = _hostname(company.get("domain"))
        if website_host and company_host and website_host == company_host:
            score = max(score, 1.0)
            signals.append("OFFICIAL_DOMAIN_EXACT")
        if application_host and company_host and (
            application_host == company_host or application_host.endswith("." + company_host)
        ):
            score = max(score, 0.90)
            signals.append("APPLICATION_DOMAIN_RELATED")
        if normalized_name and normalize_company_name(company.get("name")) == normalized_name:
            score = max(score, 0.75)
            signals.append("NORMALIZED_NAME_EXACT")
        if ats_tenant and any(str(row.get("tenant") or "").casefold() == ats_tenant
                              for row in source_by_company.get(company["id"], [])):
            score = max(score, 0.95)
            signals.append("ATS_TENANT_MATCH")
        if signals:
            scored.append((score, company, signals))
    if scored:
        score, company, signals = max(scored, key=lambda item: (item[0], item[1]["id"]))
        return {
            "company_id": company["id"],
            "confidence": score,
            "evidence": signals,
            "is_new": False,
            "name": company["name"],
            "domain": company["domain"],
        }
    if website_host:
        company_id = identity(website)
        confidence = 0.95
        evidence = ["CANDIDATE_OFFICIAL_WEBSITE"]
        domain = website
    else:
        company_id = identity("company-name", normalized_name)
        confidence = 0.55
        evidence = ["NORMALIZED_NAME_ONLY"]
        domain = ""
    return {
        "company_id": company_id,
        "confidence": confidence,
        "evidence": evidence,
        "is_new": True,
        "name": name,
        "domain": domain,
    }
