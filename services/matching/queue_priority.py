"""Fresh Job Queue Priority (MJ-REQ-119).

Authoritative requirement:
"New excellent matches should be processed before stale lower-quality jobs.
Use queue priorities."

This module implements deterministic, transparent queue prioritization combining
match quality and job freshness without mutating persisted scores or eligibility.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Literal, TypedDict


# Queue priority tier identifiers (lower index = higher priority)
QUEUE_TIERS = (
    "CRITICAL",    # Tier 0: Fresh (< 24h) + Excellent (>= 85)
    "HIGH",        # Tier 1: Fresh + Strong (75-84) OR Recent (24-72h) + Excellent (>= 85)
    "NORMAL",      # Tier 2: Recent + Strong OR Fresh + Possible (60-74)
    "DEFERRED",    # Tier 3: Aging (3-30d) with match OR Recent + Possible
    "STALE",       # Tier 4: Stale (> 30d) OR Weak/Poor match (< 60)
    "BACKGROUND",  # Tier 5: Missing/Unknown match or timestamps
)

QueueTierName = Literal["CRITICAL", "HIGH", "NORMAL", "DEFERRED", "STALE", "BACKGROUND"]
DispatchPriority = Literal["HIGH", "NORMAL", "LOW"]
MatchQualityBand = Literal["EXCEPTIONAL", "EXCELLENT", "STRONG", "POSSIBLE", "WEAK", "POOR", "UNKNOWN"]
FreshnessBand = Literal["FRESH", "RECENT", "AGING", "STALE", "UNKNOWN"]
TimestampKind = Literal["POSTED", "FIRST_SEEN", "CREATED", "UNKNOWN"]
TimestampState = Literal["KNOWN", "INVALID", "FUTURE", "UNKNOWN"]


class QueuePriorityResult(TypedDict):
    job_id: str
    queue_priority: QueueTierName
    tier: int
    dispatch_priority: DispatchPriority
    queue_weight: int
    match_score: float | None
    match_quality: MatchQualityBand
    freshness_band: FreshnessBand
    age_hours: float | None
    timestamp_source: str
    timestamp_kind: TimestampKind
    timestamp_state: TimestampState
    explanation: str
    expedited: bool
    deferred: bool


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _extract_match_score(match: Any) -> float | None:
    if isinstance(match, (int, float)) and isfinite(match) and 0 <= match <= 100:
        return float(match)
    if isinstance(match, Mapping):
        score = match.get("score")
        if score is None:
            score = match.get("match_score")
        if isinstance(score, (int, float)) and isfinite(score) and 0 <= score <= 100:
            return float(score)
    return None


def _classify_match_quality(score: float | None) -> MatchQualityBand:
    if score is None:
        return "UNKNOWN"
    if score >= 95.0:
        return "EXCEPTIONAL"
    if score >= 85.0:
        return "EXCELLENT"
    if score >= 75.0:
        return "STRONG"
    if score >= 60.0:
        return "POSSIBLE"
    if score >= 40.0:
        return "WEAK"
    return "POOR"


def _timestamp_evidence(
    value: Any, source: str, kind: TimestampKind,
) -> tuple[datetime | None, str, TimestampKind, TimestampState]:
    parsed = _parse_timestamp(value)
    return parsed, source, kind, "KNOWN" if parsed is not None else "INVALID"


def _supplied_timestamp(value: Any) -> bool:
    return value is not None and value != ""


def _extract_job_timestamp(
    job: Mapping[str, Any],
) -> tuple[datetime | None, str, TimestampKind, TimestampState]:
    """Select date evidence using this queue's conservative precedence policy.

    Explicit normalized publication evidence, then direct ``posted_at``, takes
    precedence over first-seen observation and local record creation. Invalid
    supplied evidence is retained as unknown rather than replaced by a lower
    precedence date. In particular, normalized-null with a nonempty raw source
    claim follows ``effective_job_timestamp``'s non-fallback convention.
    First-seen/created values describe observed recency, never publication.
    Local ``updated_at`` does not establish an employer publication update.
    """
    attributes = job.get("attributes")
    if isinstance(attributes, Mapping):
        reported = attributes.get("reported_posted_at")
        if _supplied_timestamp(reported):
            return _timestamp_evidence(reported, "attributes.reported_posted_at", "POSTED")
        if _supplied_timestamp(attributes.get("reported_posted_at_raw")):
            return None, "attributes.reported_posted_at_raw", "POSTED", "INVALID"

    posted_at = job.get("posted_at")
    if _supplied_timestamp(posted_at):
        return _timestamp_evidence(posted_at, "job.posted_at", "POSTED")

    if isinstance(attributes, Mapping):
        first_seen = attributes.get("first_seen_at")
        if _supplied_timestamp(first_seen):
            return _timestamp_evidence(first_seen, "attributes.first_seen_at", "FIRST_SEEN")

    created_at = job.get("created_at")
    if _supplied_timestamp(created_at):
        return _timestamp_evidence(created_at, "job.created_at", "CREATED")

    return None, "UNKNOWN", "UNKNOWN", "UNKNOWN"


def _classify_freshness(age_hours: float | None) -> FreshnessBand:
    if age_hours is None or age_hours < 0:
        return "UNKNOWN"
    if age_hours <= 24.0:
        return "FRESH"
    if age_hours <= 72.0:
        return "RECENT"
    if age_hours <= 720.0:  # 30 days
        return "AGING"
    return "STALE"


def calculate_job_queue_priority(
    job: Mapping[str, Any],
    match: Any = None,
    *,
    now: datetime | str | None = None,
) -> QueuePriorityResult:
    """Calculate queue priority for one job and match evidence.

    Combines match quality with explicitly identified date evidence:
    - New excellent matches (score >= 85, age <= 24h) receive CRITICAL queue priority (Tier 0).
    - Fresh strong matches or recent excellent matches receive HIGH priority (Tier 1).
    - Older or lower-quality jobs are deferred behind fresh opportunities.

    These tiers express internal processing preference, not an employer deadline.
    This bounded policy treats every future date as FUTURE with unknown age and
    freshness; no clock-skew allowance is assumed. Invalid or naive dates are
    INVALID. A KNOWN first-seen/created time provides observation recency while
    publication remains unknown. Raw input dates are never rewritten. These
    conservative choices do not establish route state, employer closure, or a
    source update-date contract required by the broader MJ-REF-056 capability.
    """
    if not isinstance(job, Mapping):
        raise ValueError("Job must be a mapping")

    job_id = str(job.get("id") or job.get("job_id") or "UNKNOWN")
    match_score = _extract_match_score(match)
    if match_score is None:
        # Check if score is embedded in job
        match_score = _extract_match_score(job.get("match_score"))

    match_quality = _classify_match_quality(match_score)

    current_time = _parse_timestamp(now) or datetime.now(timezone.utc)
    job_time, time_source, time_kind, time_state = _extract_job_timestamp(job)

    if job_time is not None:
        age_hours = (current_time - job_time).total_seconds() / 3600.0
        if age_hours < 0:
            age_hours = None
            time_state = "FUTURE"
    else:
        age_hours = None

    freshness = _classify_freshness(age_hours)
    date_label = {
        "POSTED": "posting", "FIRST_SEEN": "first-seen observation",
        "CREATED": "local record", "UNKNOWN": "observation",
    }[time_kind]

    # Determine Tier based on (match_quality, freshness)
    tier: int
    queue_priority: QueueTierName
    dispatch_priority: DispatchPriority
    queue_weight: int
    explanation: str

    if match_quality in {"EXCEPTIONAL", "EXCELLENT"} and freshness == "FRESH":
        tier = 0
        queue_priority = "CRITICAL"
        dispatch_priority = "HIGH"
        queue_weight = 100
        explanation = (
            f"Expedited queue priority: fresh {date_label} ({age_hours:.1f}h old) with "
            f"{match_quality.lower()} match ({match_score:.1f}%)."
        )
    elif (match_quality in {"EXCEPTIONAL", "EXCELLENT"} and freshness == "RECENT") or (
        match_quality == "STRONG" and freshness == "FRESH"
    ):
        tier = 1
        queue_priority = "HIGH"
        dispatch_priority = "HIGH"
        queue_weight = 80
        if freshness == "FRESH":
            explanation = (
                f"High queue priority: fresh {date_label} ({age_hours:.1f}h old) with "
                f"strong match ({match_score:.1f}%)."
            )
        else:
            explanation = (
                f"High queue priority: recent {date_label} ({age_hours / 24:.1f}d old) with "
                f"{match_quality.lower()} match ({match_score:.1f}%)."
            )
    elif (match_quality == "STRONG" and freshness == "RECENT") or (
        match_quality == "POSSIBLE" and freshness == "FRESH"
    ):
        tier = 2
        queue_priority = "NORMAL"
        dispatch_priority = "NORMAL"
        queue_weight = 60
        explanation = (
            f"Normal queue priority: {freshness.lower()} opportunity with "
            f"{match_quality.lower()} alignment ({match_score:.1f}%)."
        )
    elif freshness in {"AGING", "RECENT"} and match_quality in {"POSSIBLE", "STRONG"}:
        tier = 3
        queue_priority = "DEFERRED"
        dispatch_priority = "LOW"
        queue_weight = 40
        explanation = (
            f"Deferred queue priority: aging {date_label} ({age_hours / 24:.1f}d old). "
            f"Deferred behind fresher high-quality opportunities."
        )
    elif freshness == "STALE" or match_quality in {"WEAK", "POOR"}:
        tier = 4
        queue_priority = "STALE"
        dispatch_priority = "LOW"
        queue_weight = 20
        if freshness == "STALE":
            explanation = (
                f"Low queue priority: stale {date_label} ({age_hours / 24:.1f}d old). "
                f"Processed after fresh jobs."
            )
        else:
            explanation = (
                f"Low queue priority: {match_quality.lower()} match alignment ({match_score:.1f}%). "
                f"Deferred behind stronger opportunities."
            )
    else:
        # Background / Unknown
        tier = 5
        queue_priority = "BACKGROUND"
        dispatch_priority = "LOW"
        queue_weight = 10
        explanation = (
            "Background queue priority: match quality or usable recency evidence is UNKNOWN; "
            "scheduled for baseline evaluation."
        )

    if time_state == "INVALID":
        date_note = (
            f"Publication date unknown: supplied {date_label} timestamp is invalid "
            "or lacks a timezone; age is unknown."
        )
    elif time_state == "FUTURE":
        date_note = (
            f"Publication date unknown: supplied {date_label} timestamp is in the future; "
            "age is unknown."
        )
    elif time_kind == "POSTED":
        date_note = "Recency is based on the reported publication time."
    elif time_kind == "FIRST_SEEN":
        date_note = "Publication date unknown; recency is based on first-seen observation."
    elif time_kind == "CREATED":
        date_note = "Publication date unknown; recency is based on local record creation."
    else:
        date_note = "Publication date unknown; no usable observation timestamp is available."
    explanation += f" {date_note} Internal processing preference."

    return {
        "job_id": job_id,
        "queue_priority": queue_priority,
        "tier": tier,
        "dispatch_priority": dispatch_priority,
        "queue_weight": queue_weight,
        "match_score": match_score,
        "match_quality": match_quality,
        "freshness_band": freshness,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "timestamp_source": time_source,
        "timestamp_kind": time_kind,
        "timestamp_state": time_state,
        "explanation": explanation,
        "expedited": tier in (0, 1),
        "deferred": tier >= 3,
    }


def order_queue_by_fresh_priority(
    jobs: Sequence[Mapping[str, Any]],
    matches: Mapping[str, Any] | None = None,
    *,
    now: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Sort a collection of jobs deterministically by fresh job priority.

    Guarantee:
    1. Lower tier numbers appear strictly before higher tier numbers
       (Tier 0 Critical precedes Tier 1 High precedes Tier 2 Normal precedes Tier 3/4/5).
    2. Within the same tier:
       - Higher match score comes first
       - Lower age (fresher) comes first
       - Casefolded job title tie-breaker
       - Unique job ID final tie-breaker
    Zero non-deterministic jitter.
    """
    if not isinstance(jobs, (list, tuple)):
        raise ValueError("Jobs must be a sequence of mappings")

    matches_map = matches or {}
    evaluated: list[tuple[dict[str, Any], QueuePriorityResult]] = []

    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        job_id = str(job.get("id") or job.get("job_id") or "")
        match_evidence = matches_map.get(job_id)
        priority = calculate_job_queue_priority(job, match_evidence, now=now)
        # Augment copy of job dictionary with display and ordering metadata
        augmented = dict(job)
        augmented["queue_priority"] = priority["queue_priority"]
        augmented["queue_tier"] = priority["tier"]
        augmented["dispatch_priority"] = priority["dispatch_priority"]
        augmented["queue_weight"] = priority["queue_weight"]
        augmented["freshness_band"] = priority["freshness_band"]
        augmented["timestamp_source"] = priority["timestamp_source"]
        augmented["timestamp_kind"] = priority["timestamp_kind"]
        augmented["timestamp_state"] = priority["timestamp_state"]
        augmented["match_quality"] = priority["match_quality"]
        augmented["queue_explanation"] = priority["explanation"]
        augmented["expedited"] = priority["expedited"]
        augmented["deferred"] = priority["deferred"]
        evaluated.append((augmented, priority))

    def sort_key(pair: tuple[dict[str, Any], QueuePriorityResult]):
        row, p = pair
        tier = p["tier"]
        score = p["match_score"]
        neg_score = -score if score is not None else 0.0
        age = p["age_hours"]
        pos_age = age if age is not None else 9999999.0
        title = str(row.get("title") or "").casefold()
        job_id = str(row.get("id") or "")
        return (tier, neg_score, pos_age, title, job_id)

    evaluated.sort(key=sort_key)
    return [item[0] for item in evaluated]


def summarize_queue_distribution(
    ranked_jobs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Provide summary diagnostics of queue priority distribution."""
    counts = {tier: 0 for tier in QUEUE_TIERS}
    for job in ranked_jobs:
        tier = job.get("queue_priority")
        if tier in counts:
            counts[tier] += 1
    total = sum(counts.values())
    return {
        "total_queued": total,
        "expedited_count": counts["CRITICAL"] + counts["HIGH"],
        "normal_count": counts["NORMAL"],
        "deferred_count": counts["DEFERRED"] + counts["STALE"],
        "background_count": counts["BACKGROUND"],
        "tier_counts": counts,
    }


def to_operation_dispatch_params(
    priority: QueuePriorityResult,
    extra_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert queue priority to worker operation dispatch parameters.

    Compatible with `workers/dispatch.py` outbox priority case statement:
    `m.operations.c.params["dispatch_priority"].as_string() == "HIGH"`.
    """
    params = dict(extra_params or {})
    params["dispatch_priority"] = priority["dispatch_priority"]
    params["queue_tier"] = priority["tier"]
    params["queue_priority"] = priority["queue_priority"]
    params["fresh_job_priority"] = priority["expedited"]
    return params
