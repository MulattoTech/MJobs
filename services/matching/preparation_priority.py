"""Grounded admission snapshots and deterministic, aging preparation priority.

Redis is transport only. The durable category-serialized claim is authoritative.
At most five hourly promotions put any eligible waiting preparation in tier zero;
immutable enqueue time then puts it ahead of later arrivals. Running work is never
preempted. Retry delays and cancellation remain independent eligibility gates.
"""
from datetime import datetime, timezone

from services.discovery.matching import build_explanation
from services.documents.tailor import target_fingerprint
from services.matching.queue_priority import calculate_job_queue_priority


POLICY = "fresh-preparation-v1"
AGING_SECONDS = 3600


def admission_snapshot(job, profile, timestamp):
    """Freeze the queue decision and its date evidence at admission.

    Provenance is additive to the v1 snapshot. Later job edits or passing time
    do not reinterpret this evidence; only waiting-time aging changes priority.
    """
    explanation = build_explanation(job, profile)
    coverage = explanation["components"]["skill_evidence_coverage"]
    score = explanation["score"] if coverage["possible_weight"] > 0 else None
    priority = calculate_job_queue_priority(job, score, now=timestamp)
    return {"policy": POLICY, "tier": priority["tier"],
            "queue_priority": priority["queue_priority"], "match_score": score,
            "freshness_band": priority["freshness_band"], "enqueued_at": timestamp,
            "timestamp_source": priority["timestamp_source"],
            "timestamp_kind": priority["timestamp_kind"],
            "timestamp_state": priority["timestamp_state"],
            "profile_version": profile["version"], "job_fingerprint": target_fingerprint(job)}


def _instant(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def scheduling_key(row, timestamp):
    snapshot = (row["params"] or {}).get("preparation_priority") or {}
    valid = (snapshot.get("policy") == POLICY and type(snapshot.get("tier")) is int
             and 0 <= snapshot["tier"] <= 5)
    # Legacy rows keep their old timestamp and normal priority until completed.
    tier = snapshot["tier"] if valid else 2
    instant = _instant(timestamp)
    enqueued = _instant(snapshot.get("enqueued_at")) if valid else None
    enqueued = enqueued or _instant(row["updated_at"]) or instant
    waited = max(0, (instant - enqueued).total_seconds())
    return max(0, tier - int(waited // AGING_SECONDS)), enqueued, row["id"]


def public_priority(row, timestamp):
    """Expose bounded, frozen date provenance without inferring missing history.

    Older v1 snapshots retain their scheduling fields but have UNKNOWN date
    evidence. First-seen/creation recency is not publication age; future or
    invalid evidence remains unknown even after the supplied date has passed.
    """
    snapshot = (row["params"] or {}).get("preparation_priority") or {}
    if row["kind"] != "PREPARE" or snapshot.get("policy") != POLICY:
        return None
    source = snapshot.get("timestamp_source", "UNKNOWN")
    kind = snapshot.get("timestamp_kind", "UNKNOWN")
    state = snapshot.get("timestamp_state", "UNKNOWN")
    source_kinds = {
        "attributes.reported_posted_at": "POSTED",
        "attributes.reported_posted_at_raw": "POSTED",
        "job.posted_at": "POSTED",
        "attributes.first_seen_at": "FIRST_SEEN",
        "job.created_at": "CREATED",
    }
    if (not isinstance(source, str) or source_kinds.get(source) != kind
            or state not in ("KNOWN", "INVALID", "FUTURE")
            or (source == "attributes.reported_posted_at_raw" and state != "INVALID")):
        source = kind = state = "UNKNOWN"
    date_basis = {
        "POSTED": "POSTING_AGE", "FIRST_SEEN": "FIRST_SEEN_RECENCY",
        "CREATED": "RECORD_CREATION_RECENCY",
    }.get(kind, "UNKNOWN_RECENCY") if state == "KNOWN" else "UNKNOWN_RECENCY"
    return {"tier": snapshot["tier"], "effective_tier": scheduling_key(row, timestamp)[0],
            "queue_priority": snapshot["queue_priority"], "enqueued_at": snapshot["enqueued_at"],
            "aging_interval_seconds": AGING_SECONDS,
            "timestamp_source": source, "timestamp_kind": kind, "timestamp_state": state,
            "basis": f"APPROVED_SKILL_EVIDENCE_AND_{date_basis}"}
