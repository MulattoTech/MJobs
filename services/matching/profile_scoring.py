"""Compose profile selection, the legacy match baseline, and optional signals."""

import re
from collections.abc import Mapping
from math import isfinite

from services.matching.scoring_profiles import (
    RoleScoringProfileResolution,
    resolve_role_scoring_profile,
)
from services.matching.specialized_signals import score_specialized_signals
from services.discovery.skill_taxonomy import canonical_skill_id, extract_skills, skill_label


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


def _experience_contribution(job, candidate_profile, weight):
    target_labels = {}
    if isinstance(job, Mapping):
        job_skills = job.get("skills")
        for value in job_skills if isinstance(job_skills, (list, tuple, set)) else []:
            identifier = canonical_skill_id(value)
            label = skill_label(identifier) if identifier else None
            if label:
                target_labels[identifier] = label
    target_skills = set(target_labels)
    experience_skills = set()
    if isinstance(candidate_profile, Mapping):
        facts = candidate_profile.get("facts")
        if isinstance(facts, (list, tuple)):
            for fact in facts:
                if not isinstance(fact, Mapping) or fact.get("kind") not in {
                    "experience", "technology_experience", "responsibility", "project", "accomplishment",
                }:
                    continue
                text = fact.get("text")
                source = fact.get("source")
                confidence = fact.get("confidence")
                if (fact.get("verified") is not True or fact.get("user_approved") is not True
                        or type(confidence) not in (int, float) or not isfinite(confidence)
                        or confidence < 80 or not isinstance(text, str) or not text.strip()
                        or not isinstance(source, str) or not source.strip()):
                    continue
                for label in extract_skills(text).get("labels", []):
                    identifier = canonical_skill_id(label)
                    if identifier:
                        experience_skills.add(identifier)

    matched = sorted(target_skills & experience_skills)
    if target_skills and matched:
        labels = [target_labels[identifier] for identifier in matched]
        raw_score = round(100 * len(matched) / len(target_skills), 2)
        explanation = (
            f"Verified, user-approved experience facts match {len(matched)} of "
            f"{len(target_skills)} recognized job skills: {', '.join(labels[:8])}."
        )
        provenance = {"source": "USER_APPROVED_EXPERIENCE_FACTS"}
    else:
        raw_score = None
        explanation = (
            "Relevant experience is UNKNOWN because no supported skill overlap is present in verified, "
            "user-approved, source-backed experience facts; no score penalty is applied."
        )
        provenance = {"source": "UNKNOWN"}
    return {
        "dimension": "experience",
        "weight": weight,
        "raw_score": raw_score,
        "weighted_contribution": None,
        "explanation": explanation,
        "provenance": provenance,
    }


def compose_profile_scoring(job, legacy_match, resolution, *, candidate_profile=None):
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
        if _SPECIALIZED_DIMENSION_NAME.fullmatch(dimension) and dimension != "experience"
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
    experience_weight = weights.get("experience", 0.0)
    if experience_weight > 0:
        rows.append(_experience_contribution(job, candidate_profile, experience_weight))
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


def resolve_and_compose_profile_scoring(
    job,
    legacy_match,
    *,
    role_family,
    profiles_by_role_family=None,
    default_profile=None,
    candidate_profile=None,
):
    """Resolve a user-edited role profile and explain its advisory score.

    This is the reusable entry point for callers that have the selected role
    family and the available profile catalog. Profile validation and fallback
    selection stay centralized in :func:`resolve_role_scoring_profile`; score
    composition then preserves the legacy baseline and UNKNOWN semantics.
    """
    resolution = resolve_role_scoring_profile(
        role_family,
        profiles_by_role_family,
        default_profile=default_profile,
    )
    return compose_profile_scoring(
        job, legacy_match, resolution, candidate_profile=candidate_profile,
    )
