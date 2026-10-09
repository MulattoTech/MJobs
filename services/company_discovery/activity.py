"""Profile-scoped, plain-language company discovery activity."""
from __future__ import annotations

import re
from contextlib import nullcontext

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity


_ATS_REVIEW_ACTIONS = {"COMPANY_ATS_DETECTION_REVIEW"}
_IMPORT_ACTIONS = {"COMPANY_FEED_IMPORT_COMPLETE"}
_SOURCE_CREATED_ACTIONS = {"COMPANY_SOURCE_DISCOVERED"}
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
_SAFE_OPERATION_ERRORS = {
    "COMPANY_OFFICIAL_WEBSITE_REQUIRED": "An official employer website could not be identified.",
    "COMPANY_OFFICIAL_WEBSITE_NOT_FOUND": "No official employer website could be verified.",
    "COMPANY_ATS_REVIEW_REQUIRED": "The careers system needs a manual review.",
    "COMPANY_ATS_ENUMERATION_UNSUPPORTED": "This careers system is not supported yet.",
    "COMPANY_JOB_ENUMERATION_INVALID": "The employer's public job feed could not be read.",
    "COMPANY_DISCOVERY_URL_BLOCKED": "A requested page did not meet safe browsing rules.",
    "COMPANY_DISCOVERY_REDIRECT_BLOCKED": "A page redirect did not meet safe browsing rules.",
}
_TRIGGER_LABELS = {
    "PREVIOUSLY_UNSEEN_COMPANY": "Previously unseen company",
    "HIGH_MATCH_JOB": "High-match job",
}


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


def _event(event_id: str, timestamp: object, event_type: str, summary: str,
           details: dict | None = None) -> dict:
    event = {"id": event_id, "timestamp": timestamp,
             "event_type": event_type, "summary": summary}
    if details is not None:
        event["details"] = details
    return event


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
        requests = list(connection.execute(select(
            m.company_discovery_requests.c.id,
            m.company_discovery_requests.c.company_id,
            m.company_discovery_requests.c.created_at,
            m.company_discovery_requests.c.reasons,
            m.company_discovery_requests.c.operation_id,
        ).where(m.company_discovery_requests.c.profile_id == profile_id)
          .order_by(m.company_discovery_requests.c.created_at.desc(),
                    m.company_discovery_requests.c.id.desc()).limit(limit)).mappings())
        request_company_ids = set(connection.execute(select(
            m.company_discovery_requests.c.company_id,
        ).where(m.company_discovery_requests.c.profile_id == profile_id)).scalars())
        associated_company_ids = profile_company_ids | request_company_ids

        operation_ids = {row["operation_id"] for row in requests if row["operation_id"]}
        operations = {}
        if operation_ids:
            operations = {row["id"]: row for row in connection.execute(select(
                m.operations.c.id, m.operations.c.state, m.operations.c.error,
                m.operations.c.attempt_count, m.operations.c.updated_at,
                m.operations.c.last_attempt_at,
            ).where(m.operations.c.id.in_(operation_ids),
                    m.operations.c.kind == "COMPANY_DISCOVER")).mappings()}

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
                event_type, summary = "COMPANY_SOURCE_ENROLLED", f"Added direct employer source for {company}"
            result.append(_event(row["id"], row["timestamp"], event_type, summary))

        for request in requests:
            company = company_names.get(request["company_id"]) or "the company"
            operation = operations.get(request["operation_id"])
            if operation:
                state = operation["state"] if operation["state"] in {
                    "PENDING", "QUEUED", "RUNNING", "SUCCEEDED", "COMPLETED",
                    "FAILED", "BLOCKED", "DEAD_LETTERED", "CANCELLED", "DISCARDED",
                } else "UNKNOWN"
                raw_error = operation["error"]
                error = _SAFE_OPERATION_ERRORS.get(raw_error)
                if error is None and isinstance(raw_error, str) and raw_error:
                    error = "The discovery attempt ended with an error that needs review."
                timestamp = operation["updated_at"] or request["created_at"]
                event_type = "COMPANY_DISCOVERY_ATTEMPT"
                summary = ("Company discovery completed." if state in {"SUCCEEDED", "COMPLETED"}
                           else "Company discovery attempt needs review." if state in {
                               "FAILED", "BLOCKED", "DEAD_LETTERED", "CANCELLED", "DISCARDED", "UNKNOWN"}
                           else "Company discovery is in progress.")
            else:
                state = "NEEDS_REVIEW"
                error = None
                timestamp = request["created_at"]
                event_type = "COMPANY_DISCOVERY_ATTEMPT"
                summary = "Company discovery is waiting for review."

            reasons = request["reasons"] if isinstance(request["reasons"], list) else []
            triggers = [_TRIGGER_LABELS[reason] for reason in reasons
                        if isinstance(reason, str) and reason in _TRIGGER_LABELS]
            latest_review = None
            if request["company_id"] in associated_company_ids:
                latest_review = connection.execute(select(
                    m.audit.c.payload,
                ).where(m.audit.c.entity_id == request["company_id"],
                        m.audit.c.action.in_(_ATS_REVIEW_ACTIONS)).order_by(
                    m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).mappings().first()
            review_payload = latest_review["payload"] if latest_review and isinstance(
                latest_review["payload"], dict) else {}
            provider = _PROVIDER_LABELS.get(str(review_payload.get("provider") or "").casefold())
            confidence = review_payload.get("confidence")
            confidence_basis = "ATS detection" if (
                review_payload.get("status") == "MATCH" and provider
                and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                and 0 <= confidence <= 1
            ) else None
            if confidence_basis is None:
                confidence = None
                provider = None
            else:
                confidence = float(confidence)
            evidence = []
            if provider:
                evidence.append("ATS detected")
            verified_domain = connection.execute(select(
                m.company_domains.c.verified,
            ).where(m.company_domains.c.company_id == request["company_id"],
                    m.company_domains.c.verified.is_(True)).limit(1)).scalar_one_or_none()
            if verified_domain:
                evidence.append("Official domain verified")
                if confidence is None:
                    confidence, confidence_basis = 0.99, "verified domain"
            source_created = False
            results = None
            if state in {"SUCCEEDED", "COMPLETED"}:
                result_start = operation["last_attempt_at"] if operation else None
                result_end = operation["updated_at"] if operation else None
                source_event = connection.execute(select(
                    m.audit.c.payload,
                ).where(
                    m.audit.c.entity_id == request["company_id"],
                    m.audit.c.action.in_(_SOURCE_CREATED_ACTIONS),
                    m.audit.c.timestamp >= (result_start or request["created_at"]),
                    m.audit.c.timestamp <= (result_end or request["created_at"]),
                ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).mappings().first()
                source_created = source_event is not None
                result_values = {}
                if source_event and isinstance(source_event["payload"], dict):
                    payload = source_event["payload"]
                    result_values["jobs_enumerated"] = _count(payload.get("jobs_enumerated"))
                    result_values["jobs_imported"] = _count(payload.get("jobs_imported"))
                feed_result = connection.execute(select(
                    m.audit.c.payload,
                ).where(
                    m.audit.c.entity_id == request["company_id"],
                    m.audit.c.action == "COMPANY_FEED_IMPORT_COMPLETE",
                    m.audit.c.timestamp >= (result_start or request["created_at"]),
                    m.audit.c.timestamp <= (result_end or request["created_at"]),
                    m.audit.c.payload["profile_id"].as_string() == profile_id,
                ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).mappings().first()
                if feed_result and isinstance(feed_result["payload"], dict):
                    payload = feed_result["payload"]
                    result_values["jobs_enumerated"] = _count(payload.get("jobs_enumerated"))
                    result_values["jobs_imported"] = _count(payload.get("jobs_imported"))
                    result_values["matched_jobs"] = _count(payload.get("matched_jobs"))
                if result_values:
                    results = {
                        "jobs_enumerated": result_values.get("jobs_enumerated"),
                        "jobs_imported": result_values.get("jobs_imported"),
                        "matched_jobs": result_values.get("matched_jobs"),
                    }
            details = {
                "company": company,
                "triggers": triggers,
                "attempt_state": state,
                "attempt_count": _count(operation["attempt_count"]) if operation else 0,
                "ats_provider": provider,
                "confidence": confidence,
                "confidence_basis": confidence_basis,
                "evidence": evidence,
                "source_created": source_created,
                "results": results,
                "error": error,
            }
            result.append(_event(
                identity("company-discovery-activity", profile_id, request["id"], "attempt"),
                timestamp, event_type, summary, details,
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
    # Keep durable identifiers for deterministic ordering, but never expose raw
    # audit/source IDs in the user-facing activity projection.
    return [{key: value for key, value in event.items() if key != "id"}
            for event in result[:limit]]
