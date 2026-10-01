"""Company-domain normalization and evidence-aware persistence."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, upsert


AGGREGATOR_HOSTS = {
    "linkedin.com", "indeed.com", "crunchbase.com", "glassdoor.com",
    "ziprecruiter.com", "monster.com", "careerbuilder.com", "simplyhired.com",
}
VERIFIED_METHODS = {
    "FIRST_PARTY_JOB_LINK", "VERIFIED_EXISTING_DOMAIN", "ATS_COMPANY_METADATA",
    "OFFICIAL_CAREER_REDIRECT", "OFFICIAL_STRUCTURED_METADATA",
}
METHOD_CONFIDENCE = {
    "FIRST_PARTY_JOB_LINK": 0.99,
    "VERIFIED_EXISTING_DOMAIN": 0.99,
    "ATS_COMPANY_METADATA": 0.98,
    "OFFICIAL_CAREER_REDIRECT": 0.96,
    "OFFICIAL_STRUCTURED_METADATA": 0.92,
    "CONFIGURED_WEB_SEARCH": 0.65,
    "PUBLIC_METADATA": 0.55,
    "CANDIDATE_DOMAIN_UNVERIFIED": 0.45,
}


def normalize_domain(value):
    """Return a normalized public-looking hostname without fetching it."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")
        # User-info, unusual ports, local names, and non-web schemes are not company domains.
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return ""
        if parsed.port not in (None, 80, 443) or "." not in host:
            return ""
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return ""
        return host
    except ValueError:
        return ""


def is_aggregator_domain(host):
    host = normalize_domain(host)
    return any(host == domain or host.endswith("." + domain) for domain in AGGREGATOR_HOSTS)


def resolve_official_domain(candidates):
    """Rank sourced domain candidates and preserve ties as ambiguous."""
    blocked_sources = {"AGGREGATOR", "DIRECTORY", "STAFFING_AGENCY", "LINKEDIN", "INDEED", "CRUNCHBASE"}
    usable = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        host = normalize_domain(candidate.get("domain") or candidate.get("url"))
        method = str(candidate.get("method") or "").upper()
        source_type = str(candidate.get("source_type") or "").upper()
        if (not host or is_aggregator_domain(host) or source_type in blocked_sources
                or method not in METHOD_CONFIDENCE):
            continue
        usable.append({"domain": host, "method": method, "confidence": METHOD_CONFIDENCE[method],
                       "evidence": list(candidate.get("evidence") or [])})
    if not usable:
        return {"status": "NOT_FOUND", "domain": "", "confidence": 0.0, "evidence": []}
    highest = max(row["confidence"] for row in usable)
    best = [row for row in usable if row["confidence"] == highest]
    domains = sorted({row["domain"] for row in best})
    if len(domains) > 1:
        return {"status": "AMBIGUOUS", "domain": "", "confidence": highest,
                "evidence": ["EQUAL_RANK_DOMAIN_CANDIDATES"], "candidates": domains}
    winner = best[0]
    return {"status": "VERIFIED" if highest >= 0.90 else "CANDIDATE",
            **winner}


def record_company_domain(store, company_id, value, method="CANDIDATE_DOMAIN_UNVERIFIED", evidence=None):
    """Persist domain evidence; weaker observations never downgrade a verified record."""
    host = normalize_domain(value)
    method = str(method or "").upper()
    if not host or is_aggregator_domain(host):
        return None
    if method not in METHOD_CONFIDENCE:
        raise ValueError("COMPANY_DOMAIN_EVIDENCE_METHOD_INVALID")
    verified = method in VERIFIED_METHODS
    confidence = METHOD_CONFIDENCE[method]
    evidence = list(evidence or [])
    now = datetime.now(timezone.utc).isoformat()
    record_id = identity(company_id, host)
    with store.engine.begin() as connection:
        current = connection.execute(select(m.company_domains).where(
            m.company_domains.c.company_id == company_id,
            m.company_domains.c.domain == host,
        )).mappings().one_or_none()
        if current:
            if current["verified"] and not verified:
                return dict(current)
            data = {
                "id": current["id"], "company_id": company_id, "domain": host,
                "confidence": max(float(current["confidence"]), confidence),
                "verified": bool(current["verified"] or verified),
                "verification_method": method if verified else current["verification_method"],
                "first_seen_at": current["first_seen_at"],
                "last_verified_at": now if verified else current["last_verified_at"],
                "evidence": list(dict.fromkeys(list(current["evidence"] or []) + evidence)),
            }
        else:
            data = {
                "id": record_id, "company_id": company_id, "domain": host,
                "confidence": confidence, "verified": verified,
                "verification_method": method,
                "first_seen_at": now, "last_verified_at": now if verified else None,
                "evidence": evidence,
            }
        upsert(connection, m.company_domains, data, replace=True)
    return data
