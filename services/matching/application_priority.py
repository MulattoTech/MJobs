"""Unified Application Priority and Divergence Engine (MJ-REQ-118).

Authoritative requirement:
"Match score and priority are not identical.

Priority may consider:
match
posting age
salary
company preference
direct employer availability
application complexity
source freshness
expiration risk"

This module unifies transparent 8-dimension application priority scoring with
mathematical divergence analysis and fresh-job queue prioritization (MJ-REQ-119).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Literal, TypedDict

from services.discovery.application_priority import (
    explain_application_priority,
    _FACTORS,
    _OPTIONAL_FACTORS,
    _OPTIONAL_SENIORITY_FACTOR,
)
from services.matching.queue_priority import (
    calculate_job_queue_priority,
    order_queue_by_fresh_priority,
    QueuePriorityResult,
)


DivergenceClassification = Literal[
    "MATCH_DOMINANT",
    "PRIORITY_BOOSTED",
    "STRONGLY_ALIGNED",
    "MODERATELY_ALIGNED",
    "UNKNOWN",
]


class DivergenceAnalysis(TypedDict):
    match_score: float | None
    priority_score: float | None
    delta: float | None  # priority_score - match_score
    classification: DivergenceClassification
    summary: str
    top_boosters: list[dict[str, Any]]
    top_drags: list[dict[str, Any]]


class UnifiedJobPriorityResult(TypedDict):
    job_id: str
    match_score: float | None
    priority_score: float | None
    priority_band: str
    coverage_percent: float
    factors: list[dict[str, Any]]
    unknowns: list[str]
    divergence: DivergenceAnalysis
    queue: QueuePriorityResult
    advisory: bool
    affects_match_score: bool


def explain_priority_divergence(
    match_score: float | None,
    priority_score: float | None,
    factors: Sequence[Mapping[str, Any]] | None = None,
) -> DivergenceAnalysis:
    """Analyze and explain mathematical divergence between Match Score and Priority Score.

    Match score measures qualifications and skill evidence.
    Application priority measures urgency, feasibility, preference, and timing.
    They are fundamentally distinct signals.
    """
    if match_score is None or priority_score is None:
        return {
            "match_score": match_score,
            "priority_score": priority_score,
            "delta": None,
            "classification": "UNKNOWN",
            "summary": "Divergence cannot be computed because match score or priority score is UNKNOWN.",
            "top_boosters": [],
            "top_drags": [],
        }

    delta = round(priority_score - match_score, 2)
    factors_list = [f for f in (factors or []) if isinstance(f, Mapping) and f.get("status") == "KNOWN"]

    boosters: list[dict[str, Any]] = []
    drags: list[dict[str, Any]] = []

    for f in factors_list:
        score = f.get("score")
        if isinstance(score, (int, float)) and isfinite(score):
            if score > match_score:
                boosters.append({
                    "key": f.get("key"),
                    "label": f.get("label"),
                    "score": score,
                    "differential": round(score - match_score, 2),
                    "explanation": f.get("explanation"),
                })
            elif score < match_score:
                drags.append({
                    "key": f.get("key"),
                    "label": f.get("label"),
                    "score": score,
                    "differential": round(score - match_score, 2),
                    "explanation": f.get("explanation"),
                })

    boosters.sort(key=lambda b: -b["differential"])
    drags.sort(key=lambda d: d["differential"])

    classification: DivergenceClassification
    summary: str

    if delta <= -15.0:
        classification = "MATCH_DOMINANT"
        top_drag_labels = [d["label"] for d in drags[:2]]
        drag_text = f" caused by {', '.join(top_drag_labels)}" if top_drag_labels else ""
        summary = (
            f"Match-dominant ({match_score:.1f}% match vs {priority_score:.1f}% priority, delta {delta:+.1f}): "
            f"High qualification alignment is offset by operational friction{drag_text}."
        )
    elif delta >= 15.0:
        classification = "PRIORITY_BOOSTED"
        top_booster_labels = [b["label"] for b in boosters[:2]]
        boost_text = f" driven by {', '.join(top_booster_labels)}" if top_booster_labels else ""
        summary = (
            f"Priority-boosted ({priority_score:.1f}% priority vs {match_score:.1f}% match, delta {delta:+.1f}): "
            f"Moderate match is elevated by favorable handling conditions{boost_text}."
        )
    elif abs(delta) <= 5.0:
        classification = "STRONGLY_ALIGNED"
        summary = (
            f"Strongly aligned ({match_score:.1f}% match vs {priority_score:.1f}% priority, delta {delta:+.1f}): "
            f"Application priority and match score reinforce each other."
        )
    else:
        classification = "MODERATELY_ALIGNED"
        summary = (
            f"Moderately aligned ({match_score:.1f}% match vs {priority_score:.1f}% priority, delta {delta:+.1f}): "
            f"Priority moderately tracks qualification evidence."
        )

    return {
        "match_score": match_score,
        "priority_score": priority_score,
        "delta": delta,
        "classification": classification,
        "summary": summary,
        "top_boosters": boosters[:3],
        "top_drags": drags[:3],
    }


def evaluate_job_priority_full(
    job: Mapping[str, Any],
    match: Any = None,
    *,
    now: datetime | str | None = None,
    signals: Mapping[str, Any] | None = None,
    applied_preferences: list[Mapping[str, Any]] | None = None,
) -> UnifiedJobPriorityResult:
    """Evaluate unified application priority, divergence, and queue tier in one pass."""
    if not isinstance(job, Mapping):
        raise ValueError("Job must be a mapping")

    job_id = str(job.get("id") or job.get("job_id") or "UNKNOWN")
    current_time = (
        now if isinstance(now, datetime)
        else datetime.fromisoformat(now) if isinstance(now, str)
        else datetime.now(timezone.utc)
    )

    # 1. 8-dimension application priority breakdown
    priority_signals = dict(signals or {})
    match_score = None
    if isinstance(match, (int, float)) and isfinite(match):
        match_score = float(match)
    elif isinstance(match, Mapping):
        score_val = match.get("score", match.get("match_score"))
        if isinstance(score_val, (int, float)) and isfinite(score_val):
            match_score = float(score_val)

    if match_score is not None:
        priority_signals.setdefault("match_score", match_score)

    priority_result = explain_application_priority(
        priority_signals, now=current_time, applied_preferences=applied_preferences,
    )
    computed_priority_score = priority_result.get("priority_score")

    # 2. Mathematical divergence analysis
    divergence = explain_priority_divergence(
        match_score, computed_priority_score, priority_result.get("factors"),
    )

    # 3. Fresh job queue priority (MJ-REQ-119)
    queue = calculate_job_queue_priority(job, match, now=current_time)

    return {
        "job_id": job_id,
        "match_score": match_score,
        "priority_score": computed_priority_score,
        "priority_band": priority_result.get("priority_band", "UNKNOWN"),
        "coverage_percent": priority_result.get("coverage_percent", 0.0),
        "factors": priority_result.get("factors", []),
        "unknowns": priority_result.get("unknowns", []),
        "divergence": divergence,
        "queue": queue,
        "advisory": True,
        "affects_match_score": False,
    }
