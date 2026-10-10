"""Match Weight Configuration Engine (MJ-REQ-117).

Authoritative specification:
"Allow advanced configuration.
Example:
title               20
skills              25
experience          15
location            15
salary              10
education            5
company              5
industry             5

Do not hard-code this as the only model."

Provides advanced configurable weighting models, predefined industry/role presets,
mathematical normalization, validation, and seamless adapter mapping into
the 12-dimension matching components engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Mapping, Sequence, TypedDict

from services.matching.components import DimensionName, MATCH_DIMENSIONS


CONFIGURABLE_DIMENSIONS = (
    "title",
    "skills",
    "experience",
    "location",
    "salary",
    "education",
    "company",
    "industry",
)

DimensionWeightKey = str


# Canonical Presets
PRESET_BALANCED_DEFAULT: dict[str, float] = {
    "title": 20.0,
    "skills": 25.0,
    "experience": 15.0,
    "location": 15.0,
    "salary": 10.0,
    "education": 5.0,
    "company": 5.0,
    "industry": 5.0,
}

PRESET_TECHNICAL_SKILLS_FOCUS: dict[str, float] = {
    "title": 15.0,
    "skills": 40.0,
    "experience": 15.0,
    "location": 10.0,
    "salary": 10.0,
    "education": 5.0,
    "company": 2.5,
    "industry": 2.5,
}

PRESET_SENIORITY_EXPERIENCE_FOCUS: dict[str, float] = {
    "title": 20.0,
    "skills": 20.0,
    "experience": 30.0,
    "location": 10.0,
    "salary": 10.0,
    "education": 5.0,
    "company": 2.5,
    "industry": 2.5,
}

PRESET_REMOTE_COMPENSATION_FOCUS: dict[str, float] = {
    "title": 15.0,
    "skills": 20.0,
    "experience": 10.0,
    "location": 25.0,
    "salary": 20.0,
    "education": 5.0,
    "company": 2.5,
    "industry": 2.5,
}

PRESETS: dict[str, tuple[str, dict[str, float]]] = {
    "balanced_default": ("Balanced Default (Canonical Spec)", PRESET_BALANCED_DEFAULT),
    "technical_skills": ("Technical Skills Focus", PRESET_TECHNICAL_SKILLS_FOCUS),
    "seniority_experience": ("Seniority & Experience Focus", PRESET_SENIORITY_EXPERIENCE_FOCUS),
    "remote_compensation": ("Remote & Compensation Focus", PRESET_REMOTE_COMPENSATION_FOCUS),
}


@dataclass(frozen=True)
class WeightModel:
    """Configured weight distribution across core match dimensions."""

    title: float = 20.0
    skills: float = 25.0
    experience: float = 15.0
    location: float = 15.0
    salary: float = 10.0
    education: float = 5.0
    company: float = 5.0
    industry: float = 5.0

    def __post_init__(self) -> None:
        for dim in CONFIGURABLE_DIMENSIONS:
            val = getattr(self, dim)
            if not isinstance(val, (int, float)) or not isfinite(val) or val < 0.0:
                raise ValueError(f"Weight for '{dim}' must be a non-negative finite number, got {val}")

    @property
    def total_weight(self) -> float:
        return sum(getattr(self, dim) for dim in CONFIGURABLE_DIMENSIONS)

    def to_dict(self) -> dict[str, float]:
        return {dim: float(getattr(self, dim)) for dim in CONFIGURABLE_DIMENSIONS}

    def normalized(self) -> "WeightModel":
        """Return a new WeightModel scaled to sum to exactly 100.0%."""
        total = self.total_weight
        if total <= 0:
            raise ValueError("Cannot normalize weights with zero total weight")
        scale = 100.0 / total
        scaled = {dim: round(getattr(self, dim) * scale, 2) for dim in CONFIGURABLE_DIMENSIONS}
        # Correct rounding drift on last element
        diff = round(100.0 - sum(scaled.values()), 2)
        scaled["title"] = round(scaled["title"] + diff, 2)
        return WeightModel(**scaled)


def validate_weights(weights: Mapping[str, Any]) -> dict[str, float]:
    """Validate weight mapping and populate defaults for omitted dimensions."""
    if not isinstance(weights, Mapping):
        raise ValueError("Weights must be a mapping")

    result: dict[str, float] = {}
    for dim in CONFIGURABLE_DIMENSIONS:
        if dim not in weights:
            result[dim] = PRESET_BALANCED_DEFAULT[dim]
            continue
        val = weights[dim]
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not isfinite(val) or val < 0.0:
            raise ValueError(f"Weight for dimension '{dim}' must be non-negative finite number")
        result[dim] = float(val)

    if sum(result.values()) <= 0:
        raise ValueError("Total weight across all dimensions must be greater than zero")

    return result


def normalize_weights(weights: Mapping[str, Any]) -> dict[str, float]:
    """Normalize input weights so their sum equals exactly 100.0."""
    valid = validate_weights(weights)
    total = sum(valid.values())
    scale = 100.0 / total
    scaled = {dim: round(valid[dim] * scale, 2) for dim in CONFIGURABLE_DIMENSIONS}
    drift = round(100.0 - sum(scaled.values()), 2)
    scaled["title"] = round(scaled["title"] + drift, 2)
    return scaled


def to_component_dimension_weights(
    weights: WeightModel | Mapping[str, Any],
) -> dict[DimensionName, float]:
    """Map high-level 8-dimension weights into the 12-dimension engine schema."""
    w_dict = weights.to_dict() if isinstance(weights, WeightModel) else validate_weights(weights)

    skills_weight = w_dict["skills"]
    # Divide skills between required (65%) and preferred (35%)
    req_skills_weight = round(skills_weight * 0.65, 2)
    pref_skills_weight = round(skills_weight * 0.35, 2)

    return {
        "title_similarity": w_dict["title"],
        "required_skills": req_skills_weight,
        "preferred_skills": pref_skills_weight,
        "experience": w_dict["experience"],
        "education": w_dict["education"],
        "certifications": 5.0,
        "remote_location": w_dict["location"],
        "salary": w_dict["salary"],
        "employment_type": 5.0,
        "industry": w_dict["industry"],
        "company_preference": w_dict["company"],
        "negative_requirements": 0.0,
    }


class WeightConfigurationSummary(TypedDict):
    preset_name: str | None
    weights: dict[str, float]
    total_weight: float
    is_normalized: bool
    component_weights: dict[str, float]


def summarize_weight_configuration(
    weights: Mapping[str, Any],
    preset_name: str | None = None,
) -> WeightConfigurationSummary:
    """Produce a complete telemetry/UI summary of a weight configuration."""
    valid = validate_weights(weights)
    total = round(sum(valid.values()), 2)
    is_norm = abs(total - 100.0) < 0.01
    comp_weights = to_component_dimension_weights(valid)

    return {
        "preset_name": preset_name,
        "weights": valid,
        "total_weight": total,
        "is_normalized": is_norm,
        "component_weights": {k: float(v) for k, v in comp_weights.items()},
    }


__all__ = [
    "CONFIGURABLE_DIMENSIONS",
    "PRESETS",
    "PRESET_BALANCED_DEFAULT",
    "PRESET_REMOTE_COMPENSATION_FOCUS",
    "PRESET_SENIORITY_EXPERIENCE_FOCUS",
    "PRESET_TECHNICAL_SKILLS_FOCUS",
    "WeightModel",
    "normalize_weights",
    "summarize_weight_configuration",
    "to_component_dimension_weights",
    "validate_weights",
]
