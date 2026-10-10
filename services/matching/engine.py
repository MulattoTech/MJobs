"""Job Matching Engine (MJ-REQ-112).

Authoritative specification:
"Every job receives a transparent score.
Example:
91/100 — Excellent Match"

Canonical Categories (docs/spec/areas/matching_ai.md Section 116):
- 95–100: Exceptional Match
- 85–94:  Excellent Match
- 75–84:  Strong Match
- 60–74:  Possible Match
- 40–59:  Weak Match
- 0–39:   Poor Match

This module coordinates multi-dimensional match evaluation, honest UNKNOWN
semantics, transparent category classification, zero-jitter deterministic sorting,
and advisory disclosures.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, Literal, TypedDict

from services.matching.components import (
    DEFAULT_DIMENSION_WEIGHTS,
    DimensionName,
    MatchComponentsResult,
    evaluate_match_components,
)


MatchCategory = Literal[
    "EXCEPTIONAL",
    "EXCELLENT",
    "STRONG",
    "POSSIBLE",
    "WEAK",
    "POOR",
    "INCOMPLETE",
]

CATEGORY_THRESHOLDS: tuple[tuple[float, MatchCategory, str], ...] = (
    (95.0, "EXCEPTIONAL", "Exceptional Match"),
    (85.0, "EXCELLENT", "Excellent Match"),
    (75.0, "STRONG", "Strong Match"),
    (60.0, "POSSIBLE", "Possible Match"),
    (40.0, "WEAK", "Weak Match"),
    (0.0, "POOR", "Poor Match"),
)


def classify_match_category(score: float | None) -> tuple[MatchCategory, str]:
    """Classify a numeric match score (0-100) into canonical match categories."""
    if score is None or not isfinite(score):
        return "INCOMPLETE", "Incomplete Match"

    clamped = max(0.0, min(100.0, float(score)))
    for threshold, category, label in CATEGORY_THRESHOLDS:
        if clamped >= threshold:
            return category, label

    return "POOR", "Poor Match"


def format_match_score(score: float | None) -> str:
    """Format match score into canonical requirement example: '91/100 — Excellent Match'."""
    if score is None or not isfinite(score):
        return "Incomplete — Insufficient Data"

    clamped = max(0.0, min(100.0, float(score)))
    _, label = classify_match_category(clamped)
    # If integer, format without decimals, else with 1 decimal place
    score_str = f"{int(round(clamped))}" if abs(clamped - round(clamped)) < 0.05 else f"{clamped:.1f}"
    return f"{score_str}/100 — {label}"


class JobMatchResult(TypedDict):
    job_id: str | None
    score: float | None
    score_max: int
    score_formatted: str
    category: MatchCategory
    category_label: str
    advisory: bool
    coverage_percent: float
    known_dimensions: list[str]
    unknown_dimensions: list[str]
    negative_signals_triggered: list[str]
    breakdown: MatchComponentsResult
    recommendation: str
    provenance: dict[str, Any]


def _build_recommendation(
    score: float | None,
    category: MatchCategory,
    negative_signals: list[str],
    coverage: float,
) -> str:
    if negative_signals:
        return (
            f"Caution: Triggers dealbreaker avoidance term(s): {', '.join(negative_signals)}. "
            "Review carefully before considering application."
        )
    if coverage < 50.0:
        return (
            "Low signal coverage: more candidate facts or job details are needed for full evaluation."
        )
    if category in ("EXCEPTIONAL", "EXCELLENT"):
        return "High alignment: recommended for prioritized application preparation."
    if category == "STRONG":
        return "Strong potential fit: satisfies core qualifications with minor gaps."
    if category == "POSSIBLE":
        return "Moderate alignment: review qualifications and tailor resume for missing requirements."
    return "Low alignment: significant qualification or preference mismatches detected."


def score_job_match(
    job: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    weights: Mapping[DimensionName, float] | None = None,
    company_preferences: Mapping[str, str] | None = None,
) -> JobMatchResult:
    """Compute transparent match score and categorization for a job and profile."""
    if not isinstance(job, Mapping):
        raise ValueError("Job must be a mapping")
    if not isinstance(profile, Mapping):
        raise ValueError("Profile must be a mapping")

    components_result = evaluate_match_components(
        job,
        profile,
        weights=weights,
        company_preferences=company_preferences,
    )

    score = components_result["score"]
    category, label = classify_match_category(score)
    formatted = format_match_score(score)
    neg_signals = components_result.get("negative_signals_triggered", [])
    coverage = components_result.get("coverage_percent", 0.0)

    recommendation = _build_recommendation(score, category, neg_signals, coverage)

    job_id = job.get("id")
    return {
        "job_id": str(job_id) if job_id is not None else None,
        "score": score,
        "score_max": 100,
        "score_formatted": formatted,
        "category": category,
        "category_label": label,
        "advisory": True,
        "coverage_percent": coverage,
        "known_dimensions": components_result.get("known_dimensions", []),
        "unknown_dimensions": components_result.get("unknown_dimensions", []),
        "negative_signals_triggered": neg_signals,
        "breakdown": components_result,
        "recommendation": recommendation,
        "provenance": {
            "engine": "mjobs-matching-engine-v1",
            "scoring_rule": "MJ-REQ-112",
            "dimensions_evaluated": len(components_result.get("dimensions", [])),
        },
    }


def batch_score_job_matches(
    jobs: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    *,
    weights: Mapping[DimensionName, float] | None = None,
    company_preferences: Mapping[str, str] | None = None,
    sort_descending: bool = True,
) -> list[JobMatchResult]:
    """Score a batch of jobs with deterministic, jitter-free sorting."""
    scored: list[JobMatchResult] = [
        score_job_match(job, profile, weights=weights, company_preferences=company_preferences)
        for job in jobs
    ]

    if sort_descending:
        # Deterministic primary key: score (descending, None last); secondary key: job_id (ascending)
        scored.sort(
            key=lambda res: (
                -(res["score"] if res["score"] is not None else -1.0),
                res["job_id"] or "",
            )
        )

    return scored
