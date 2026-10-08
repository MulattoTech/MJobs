"""Profile-scoped, plain-language company discovery activity."""
from __future__ import annotations

import re
from contextlib import nullcontext

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity


_ATS_REVIEW_ACTIONS = {"COMPANY_ATS_DETECTION_REVIEW"}
_IMPORT_ACTIONS = {"COMPANY_FEED_IMPORT_COMPLETE"}
_PROVIDER_LABELS = {
    "adp": "ADP Recruiting",
    "ashby": "Ashby",
    "bamboohr": "BambooHR",
    "breezy": "Breezy HR",
    "dayforce": "Dayforce",
    "greenhouse": "Greenhouse",
    "icims": "iCIMS",
    "jobvite": "Jobvite",
    "jazzhr": "JazzHR",
    "lever": "Lever",
    "mock-ats": "the configured test ATS",
    "oracle-taleo": "Oracle Taleo",
    "recruitee": "Recruitee",
    "smartrecruiters": "SmartRecruiters",
    "successfactors": "SAP SuccessFactors",
    "teamtailor": "Teamtailor",
    "workable": "Workable",
    "workday": "Workday",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_TAG_LIKE = re.compile(r"<[^>]*>")


def _plain_name(value: object) -> str:
    """Reduce user/source supplied names to the UI's plain-text name alphabet."""
    if not isinstance(value, str):
        return ""
    if "<" in value or ">" in value:
        value = value.split("<", 1)[0].split(">", 1)[0]
    value = _CONTROL_CHARS.sub(" ", value)
    value = _TAG_LIKE.sub(" ", value)
    value = "".join(char if char.isalnum() or char in " .&'()_-" else " " for char in value)
    value = " ".join(value.split()).strip(" .")[:80]
    if not value or not value[0].isalnum():
        return "Company"
    return value


def _count(value: object) -> int | None:
    """Accept only explicit, non-negative integer counts from durable audit data."""
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        return None
    return value


def _event(event_id: str, timestamp: object, event_type: str, summary: str) -> dict:
    return {"id": event_id, "timestamp": timestamp,
            "event_type": event_type, "summary": summary}


def list_company_discovery_activity(store, profile_id: str, *, limit: int = 100) -> list[dict]:
    """Return a bounded newest-first projection of durable discovery events.

    Only companies associated with ``profile_id`` are considered. Import events
    additionally must carry that profile in their persisted import receipt. Raw
    audit payloads, source URLs, debug details, and absent counts are never returned.
    """
    store.profile(profile_id)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("COMPANY_DISCOVERY_ACTIVITY_LIMIT_INVALID")
    limit = max(1, min(limit, 200))

    context = store.engine.connect() if hasattr(store, "engine") else nullcontext(store)
    with context as connection:
        profile_activity = list(connection.execute(select(
            m.audit.c.id, m.audit.c.action, m.audit.c.timestamp, m.audit.c.payload,
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action.in_(("COMPANY_DISCOVERED", "COMPANY_SOURCE_ENROLLED")),
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings())
        profile_company_ids = set(connection.execute(select(
            m.audit.c.payload["company_id"].as_string(),
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action.in_(("COMPANY_DISCOVERED", "COMPANY_SOURCE_ENROLLED")),
        )).scalars()) - {None}
        discovered_company_ids = set(connection.execute(select(
            m.audit.c.payload["company_id"].as_string(),
        ).where(
            m.audit.c.entity_id == profile_id,
            m.audit.c.action == "COMPANY_DISCOVERED",
        )).scalars()) - {None}

        requests = list(connection.execute(select(
            m.company_discovery_requests.c.id,
            m.company_discovery_requests.c.company_id,
            m.company_discovery_requests.c.created_at,
        ).where(m.company_discovery_requests.c.profile_id == profile_id)
          .order_by(m.company_discovery_requests.c.created_at.desc(),
                    m.company_discovery_requests.c.id.desc()).limit(limit)).mappings())
        request_company_ids = set(connection.execute(select(
            m.company_discovery_requests.c.company_id,
        ).where(m.company_discovery_requests.c.profile_id == profile_id)).scalars())
        associated_company_ids = profile_company_ids | request_company_ids

        company_names = {}
        if associated_company_ids:
            company_names = {
                row["id"]: _plain_name(row["name"])
                for row in connection.execute(select(
                    m.companies.c.id, m.companies.c.name,
                ).where(m.companies.c.id.in_(associated_company_ids))).mappings()
            }

        result = []
        for row in profile_activity:
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            company = company_names.get(payload.get("company_id")) or "the company"
            if row["action"] == "COMPANY_DISCOVERED":
                event_type, summary = "COMPANY_DISCOVERED", f"Discovered {company}"
            else:
                event_type, summary = "COMPANY_SOURCE_ENROLLED", "Added a direct employer source"
            result.append(_event(row["id"], row["timestamp"], event_type, summary))

        for request in requests:
            if request["company_id"] in discovered_company_ids:
                continue
            company = company_names.get(request["company_id"]) or "the company"
            result.append(_event(
                identity("company-discovery-activity", profile_id, request["id"], "created"),
                request["created_at"], "COMPANY_DISCOVERED", f"Discovered {company}",
            ))

        if associated_company_ids:
            # Domain rows have durable first/last verification instants. Show
            # only verified official domains, without exposing the domain value.
            domains = connection.execute(select(
                m.company_domains.c.id, m.company_domains.c.company_id,
                m.company_domains.c.last_verified_at, m.company_domains.c.first_seen_at,
                m.company_domains.c.verified,
            ).where(m.company_domains.c.company_id.in_(associated_company_ids),
                    m.company_domains.c.verified.is_(True)).order_by(
                m.company_domains.c.last_verified_at.desc(),
                m.company_domains.c.id.desc()).limit(limit)).mappings()
            for row in domains:
                timestamp = row["last_verified_at"] or row["first_seen_at"]
                result.append(_event(
                    identity("company-discovery-activity", profile_id, row["id"], "domain"),
                    timestamp, "COMPANY_DOMAIN_RESOLVED", "Resolved official domain",
                ))

            reviews = connection.execute(select(
                m.audit.c.id, m.audit.c.entity_id, m.audit.c.action,
                m.audit.c.timestamp, m.audit.c.payload,
            ).where(m.audit.c.entity_id.in_(associated_company_ids),
                    m.audit.c.action.in_(_ATS_REVIEW_ACTIONS)).order_by(
                m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings()
            for row in reviews:
                payload = row["payload"] if isinstance(row["payload"], dict) else {}
                provider = _PROVIDER_LABELS.get(str(payload.get("provider") or "").casefold())
                if payload.get("status") == "MATCH" and provider:
                    result.append(_event(row["id"], row["timestamp"], "COMPANY_ATS_DETECTED",
                                         f"Detected {provider}"))

            # Feed import receipts are emitted on the company entity and include
            # an explicit profile id. Filter by that receipt before limiting.
            imports = connection.execute(select(
                m.audit.c.id, m.audit.c.entity_id, m.audit.c.timestamp,
                m.audit.c.payload,
            ).where(m.audit.c.entity_id.in_(associated_company_ids),
                    m.audit.c.action.in_(_IMPORT_ACTIONS),
                    m.audit.c.payload["profile_id"].as_string() == profile_id).order_by(
                m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(limit)).mappings()
            for row in imports:
                payload = row["payload"] if isinstance(row["payload"], dict) else {}
                imported = _count(payload.get("jobs_imported"))
                matched = _count(payload.get("matched_jobs"))
                if imported is not None:
                    summary = f"Imported {imported} active openings"
                else:
                    summary = "Feed import completed."
                result.append(_event(row["id"], row["timestamp"], "FEED_IMPORT_COMPLETE", summary))
                if matched is not None:
                    result.append(_event(
                        identity("company-discovery-activity", profile_id, row["id"], "matches"),
                        row["timestamp"], "COMPANY_FEED_MATCHES",
                        f"{matched} openings match this profile",
                    ))

    result.sort(key=lambda row: (str(row["timestamp"] or ""), row["id"]), reverse=True)
    return result[:limit]
