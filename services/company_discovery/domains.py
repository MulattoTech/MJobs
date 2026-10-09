"""Company-domain normalization and evidence-aware persistence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
from urllib.parse import urlsplit

from sqlalchemy import select, text

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
METHOD_PRIORITY = {
    method: index for index, method in enumerate((
        "FIRST_PARTY_JOB_LINK",
        "VERIFIED_EXISTING_DOMAIN",
        "ATS_COMPANY_METADATA",
        "OFFICIAL_CAREER_REDIRECT",
        "OFFICIAL_STRUCTURED_METADATA",
        "CONFIGURED_WEB_SEARCH",
        "PUBLIC_METADATA",
        "CANDIDATE_DOMAIN_UNVERIFIED",
    ))
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
        try:
            ipaddress.ip_address(host)
            return ""
        except ValueError:
            pass
        return host
    except ValueError:
        return ""


def is_aggregator_domain(host):
    host = normalize_domain(host)
    return any(host == domain or host.endswith("." + domain) for domain in AGGREGATOR_HOSTS)


def lock_domain_evidence(connection, host):
    """Serialize all evidence for one host, including a not-yet-present owner."""
    domain = normalize_domain(host)
    if not domain:
        raise ValueError("COMPANY_DOMAIN_INVALID")
    if connection.dialect.name == "postgresql":
        digest = hashlib.sha256(b"mjobs-domain-evidence-v1\0" + domain.encode("utf-8")).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        connection.execute(text("SELECT pg_advisory_xact_lock(:domain_key)"), {"domain_key": key})
    query = select(m.company_domains).where(m.company_domains.c.domain == domain).order_by(m.company_domains.c.id)
    if connection.dialect.name == "postgresql":
        query = query.with_for_update()
    return [dict(row) for row in connection.execute(query).mappings()]


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
    highest_priority = min(METHOD_PRIORITY[row["method"]] for row in usable)
    best = [row for row in usable if METHOD_PRIORITY[row["method"]] == highest_priority]
    domains = sorted({row["domain"] for row in best})
    if len(domains) > 1:
        confidence = max(row["confidence"] for row in best)
        return {"status": "AMBIGUOUS", "domain": "", "confidence": confidence,
                "evidence": ["EQUAL_RANK_DOMAIN_CANDIDATES"], "candidates": domains}
    winner = best[0]
    return {"status": "VERIFIED" if winner["confidence"] >= 0.90 else "CANDIDATE",
            **winner}


def resolve_verified_company_domain(store, company_id):
    """Select only persisted, evidenced official domains for an existing company."""
    with store.engine.connect() as connection:
        rows = connection.execute(select(m.company_domains).where(
            m.company_domains.c.company_id == company_id,
            m.company_domains.c.verified.is_(True),
        )).mappings().all()
    candidates = [
        {"domain": row["domain"], "method": row["verification_method"],
         "evidence": row["evidence"] or []}
        for row in rows
        if row["verification_method"] in VERIFIED_METHODS and row["evidence"]
    ]
    if not candidates:
        return {"status": "NOT_FOUND", "domain": "", "confidence": 0.0, "evidence": []}
    return resolve_official_domain(candidates)


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
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = next((row for row in lock_domain_evidence(connection, host)
                        if row["company_id"] == company_id), None)
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
