"""Validate explicit specialized job signals and build advisory score contributions.

Specialized values are source-provided metadata only. This module never derives a
signal from job prose and does not decide hard eligibility.
"""

import re
from math import isfinite


_DIMENSION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_SOURCE_LENGTH = 128
_MAX_EVIDENCE_REF_LENGTH = 2000
_UNKNOWN_PROVENANCE = {"source": "UNKNOWN"}


def _number(value, *, label):
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"{label} must be a finite number between 0 and 100")
    return float(value)


def _normalize_dimensions(values, *, label):
    if not isinstance(values, dict):
        raise ValueError(f"{label} must be an object")
    normalized = {}
    for dimension, value in values.items():
        if not isinstance(dimension, str) or not _DIMENSION_NAME.fullmatch(dimension):
            raise ValueError(f"Invalid specialized signal dimension in {label}")
        normalized[dimension] = _number(value, label=f"{label}.{dimension}")
    return normalized


def _unknown_signal():
    return None, dict(_UNKNOWN_PROVENANCE)


def _signal_from_job(job, dimension):
    if not isinstance(job, dict):
        return _unknown_signal()
    attributes = job.get("attributes")
    if not isinstance(attributes, dict):
        return _unknown_signal()
    specialized_signals = attributes.get("specialized_signals")
    if not isinstance(specialized_signals, dict):
        return _unknown_signal()
    signal = specialized_signals.get(dimension)
    if not isinstance(signal, dict) or set(signal) != {"score", "provenance"}:
        return _unknown_signal()

    score = signal.get("score")
    if type(score) not in (int, float) or not isfinite(score) or not 0 <= score <= 100:
        return _unknown_signal()
    provenance = signal.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) - {"source", "evidence_ref"}:
        return _unknown_signal()
    source = provenance.get("source")
    if (not isinstance(source, str) or not source.strip()
            or len(source) > _MAX_SOURCE_LENGTH):
        return _unknown_signal()

    normalized_provenance = {"source": source.strip()}
    evidence_ref = provenance.get("evidence_ref")
    if evidence_ref is not None:
        if (not isinstance(evidence_ref, str) or not evidence_ref.strip()
                or len(evidence_ref) > _MAX_EVIDENCE_REF_LENGTH):
            return _unknown_signal()
        normalized_provenance["evidence_ref"] = evidence_ref.strip()
    return float(score), normalized_provenance


def score_specialized_signals(job, *, weights, thresholds=None):
    """Return normalized score and explanations for configured specialized signals.

    Sparse, unknown signals are excluded from the denominator. If no configured
    signal is known, score is ``None`` so callers can preserve their legacy score.
    A threshold below the raw signal score is a soft zero contribution only.
    """
    normalized_weights = _normalize_dimensions(weights, label="weights")
    normalized_thresholds = _normalize_dimensions(
        {} if thresholds is None else thresholds, label="thresholds",
    )
    unknown_thresholds = set(normalized_thresholds) - set(normalized_weights)
    if unknown_thresholds:
        raise ValueError("Threshold dimensions must also have configured weights")

    rows = []
    active_weight = 0.0
    weighted_points = 0.0
    for dimension in sorted(normalized_weights):
        weight = normalized_weights[dimension]
        if weight == 0:
            continue
        raw_score, provenance = _signal_from_job(job, dimension)
        threshold = normalized_thresholds.get(dimension, 0.0)
        if raw_score is None:
            weighted_contribution = None
            explanation = (
                f"{dimension} is UNKNOWN because trustworthy normalized evidence is unavailable."
            )
        else:
            active_weight += weight
            effective_score = raw_score
            if raw_score < threshold:
                effective_score = 0.0
                explanation = (
                    "The signal is below the configured threshold and contributes zero "
                    "to this advisory score."
                )
            elif dimension == "ai_intensity":
                explanation = (
                    "The explicit normalized AI-intensity signal contributes to this advisory score."
                )
            else:
                explanation = f"The explicit normalized {dimension} signal contributes to this advisory score."
            weighted_points += weight * effective_score
            weighted_contribution = None
        rows.append({
            "dimension": dimension,
            "weight": weight,
            "raw_score": raw_score,
            "weighted_contribution": weighted_contribution,
            "explanation": explanation,
            "provenance": provenance,
        })

    if active_weight:
        score = round(weighted_points / active_weight, 2)
        for row in rows:
            if row["raw_score"] is None:
                continue
            effective_score = (
                0.0 if row["raw_score"] < normalized_thresholds.get(row["dimension"], 0.0)
                else row["raw_score"]
            )
            row["weighted_contribution"] = round(
                row["weight"] * effective_score / active_weight, 2,
            )
    else:
        score = None
    return {"score": score, "contributions": rows}
