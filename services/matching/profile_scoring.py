"""Compose profile selection, the legacy match baseline, and optional signals."""

import re
from collections.abc import Mapping
from math import isfinite

from services.matching.scoring_profiles import RoleScoringProfileResolution
from services.matching.specialized_signals import score_specialized_signals


_SPECIALIZED_DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _legacy_score(legacy_match):
    if not isinstance(legacy_match, Mapping):
        raise ValueError("Legacy match result must be an object")
    score = legacy_match.get("score")
    if type(score) not in (int, float) or not isfinite(score) or not 0 <= score <= 100:
        raise ValueError("Legacy match score must be a finite number between 0 and 100")
    return score


def _unknown_contribution(dimension, weight):
    return {
        "dimension": dimension,
        "weight": weight,
        "raw_score": None,
        "weighted_contribution": None,
        "explanation": (
            f"{dimension} is UNKNOWN because no supported normalized signal or legacy baseline exists."
        ),
        "provenance": {"source": "UNKNOWN"},
    }


def _legacy_fallback(score, resolution):
    return {
        "score": score,
        "scoring_profile": dict(resolution.explanation),
        "scoring_contributions": [{
            "dimension": "skills",
            "weight": 100.0,
            "raw_score": float(score),
            "weighted_contribution": float(score),
            "explanation": (
                "The legacy deterministic match score is preserved because no scoring profile was selected."
            ),
            "provenance": {"source": "LEGACY_MATCH_BASELINE"},
        }],
    }


def compose_profile_scoring(job, legacy_match, resolution):
    """Return one explainable advisory result from a resolved profile and match baseline.

    The resolver remains the authority for selecting and validating profile data.
    Unprofiled matches preserve their exact legacy score. In a profiled result,
    the legacy match score supplies the ``skills`` dimension unless its configured
    weight is zero. Specialized dimensions use only validated normalized metadata;
    unknown signals have null contributions and do not enter the denominator.
    """
    if not isinstance(resolution, RoleScoringProfileResolution):
        raise ValueError("A role-scoring profile resolution is required")
    legacy_score = _legacy_score(legacy_match)
    profile = resolution.profile
    if profile is None:
        return _legacy_fallback(legacy_score, resolution)

    # Preserve the backward-compatible skill baseline for sparse profiles.
    weights = dict(profile["weights"])
    weights.setdefault("skills", 100.0)
    thresholds = dict(profile["thresholds"])

    supported_weights = {
        dimension: weight for dimension, weight in weights.items()
        if _SPECIALIZED_DIMENSION_NAME.fullmatch(dimension)
    }
    unsupported_dimensions = {
        dimension: weight for dimension, weight in weights.items()
        if not _SPECIALIZED_DIMENSION_NAME.fullmatch(dimension) and weight > 0
    }
    supported_thresholds = {
        dimension: threshold for dimension, threshold in thresholds.items()
        if dimension in supported_weights
    }
    signal_result = score_specialized_signals(
        job, weights=supported_weights, thresholds=supported_thresholds,
    )
    rows = list(signal_result["contributions"])
    rows.extend(
        _unknown_contribution(dimension, weight)
        for dimension, weight in unsupported_dimensions.items()
    )

    for row in rows:
        if row["dimension"] != "skills":
            continue
        row["raw_score"] = float(legacy_score)
        row["provenance"] = {"source": "LEGACY_MATCH_BASELINE"}
        if legacy_score < thresholds.get("skills", 0.0):
            row["explanation"] = (
                "The legacy skills baseline is below the configured threshold and contributes zero "
                "to this advisory score."
            )
        else:
            row["explanation"] = (
                "The legacy deterministic match score contributes as the skills baseline."
            )

    rows.sort(key=lambda row: row["dimension"])
    active_weight = sum(row["weight"] for row in rows if row["raw_score"] is not None)
    if active_weight:
        weighted_points = 0.0
        for row in rows:
            if row["raw_score"] is None:
                continue
            threshold = thresholds.get(row["dimension"], 0.0)
            effective_score = 0.0 if row["raw_score"] < threshold else row["raw_score"]
            weighted_points += row["weight"] * effective_score
            row["weighted_contribution"] = round(
                row["weight"] * effective_score / active_weight, 2,
            )
        score = round(weighted_points / active_weight, 2)
    else:
        # An empty/unknown profile cannot erase the already computed legacy score.
        score = legacy_score

    return {
        "score": score,
        "scoring_profile": dict(resolution.explanation),
        "scoring_contributions": rows,
    }
