"""Read-only projection of the saved match score and profile attribution."""

from math import isfinite
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.matching.scoring_profiles import resolve_role_scoring_profile


_PROFILE_KEY = "role_scoring_profile"
_PROFILE_FIELDS = {"id", "version", "schema_version", "role_family", "weights", "thresholds"}


def _error(status_code: int, code: str):
    raise HTTPException(status_code=status_code, detail={"code": code})


def _number(value: Any, *, nullable=False) -> bool:
    if nullable and value is None:
        return True
    return type(value) in (int, float) and isfinite(value) and 0 <= value <= 100


def _current_resolution(criteria: Any):
    if not isinstance(criteria, dict):
        _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
    if _PROFILE_KEY not in criteria or criteria[_PROFILE_KEY] is None:
        return resolve_role_scoring_profile(None, {})

    profile = criteria[_PROFILE_KEY]
    if not isinstance(profile, dict) or set(profile) != _PROFILE_FIELDS:
        _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
    try:
        return resolve_role_scoring_profile(
            profile.get("role_family"), {profile.get("role_family"): profile},
        )
    except (TypeError, ValueError):
        _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")


def _stored_contributions(components: Any, resolution) -> list[dict]:
    if not isinstance(components, dict):
        _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
    contributions = components.get("scoring_contributions", [])
    if not isinstance(contributions, list):
        _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")

    result = []
    seen = set()
    for contribution in contributions:
        if not isinstance(contribution, dict):
            _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
        dimension = contribution.get("dimension")
        weight = contribution.get("weight")
        raw_score = contribution.get("raw_score")
        weighted = contribution.get("weighted_contribution")
        explanation = contribution.get("explanation")
        provenance = contribution.get("provenance")
        if (not isinstance(dimension, str) or not dimension.strip() or len(dimension) > 100
                or dimension in seen or not _number(weight)
                or not _number(raw_score, nullable=True) or not _number(weighted, nullable=True)
                or not isinstance(explanation, str) or not explanation.strip()
                or not isinstance(provenance, dict)
                or set(provenance) - {"source", "evidence_ref"}):
            _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
        source = provenance.get("source")
        evidence_ref = provenance.get("evidence_ref")
        if (not isinstance(source, str) or not source.strip() or len(source) > 128
                or (evidence_ref is not None and
                    (not isinstance(evidence_ref, str) or not evidence_ref.strip()
                     or len(evidence_ref) > 2000))):
            _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
        row = {
            "dimension": dimension,
            "weight": weight,
            "raw_score": raw_score,
            "weighted_contribution": weighted,
            "explanation": explanation,
            "provenance": {"source": source.strip()},
        }
        if evidence_ref is not None:
            row["provenance"]["evidence_ref"] = evidence_ref.strip()
        result.append(row)
        seen.add(dimension)

    profile = resolution.profile
    if profile is None:
        return result

    expected_weights = dict(profile["weights"])
    expected_weights.setdefault("skills", 100.0)
    for dimension, weight in sorted(expected_weights.items()):
        if weight == 0 or dimension in seen:
            continue
        result.append({
            "dimension": dimension,
            "weight": weight,
            "raw_score": None,
            "weighted_contribution": None,
            "explanation": (
                f"{dimension} is UNKNOWN because the stored match has no contribution for this dimension."
            ),
            "provenance": {"source": "UNKNOWN"},
        })
    result.sort(key=lambda row: row["dimension"])
    return result


def create_matching_explanations_router(store) -> APIRouter:
    """Build a read-only API for inspecting persisted match explanations."""
    router = APIRouter()

    @router.get(
        "/profiles/{profile_id}/search-profiles/{search_profile_id}/jobs/{job_id}/match-explanation"
    )
    def read_match_explanation(profile_id: str, search_profile_id: str, job_id: str):
        try:
            profile = store.profile(profile_id)
        except NoResultFound:
            _error(404, "MATCH_EXPLANATION_PROFILE_NOT_FOUND")

        with store.engine.connect() as connection:
            search = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == search_profile_id,
                m.search_profiles.c.profile_id == profile_id,
            )).mappings().one_or_none()
            if search is None:
                _error(404, "MATCH_EXPLANATION_SEARCH_NOT_FOUND")

            job = connection.execute(select(m.jobs).where(
                m.jobs.c.id == job_id,
            )).mappings().one_or_none()
            if job is None:
                _error(404, "MATCH_EXPLANATION_JOB_NOT_FOUND")

            match = connection.execute(select(m.matches).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == profile_id,
                m.matches.c.profile_version == profile["version"],
            )).mappings().one_or_none()
            if match is None:
                older_match = connection.execute(select(m.matches.c.id).where(
                    m.matches.c.job_id == job_id,
                    m.matches.c.profile_id == profile_id,
                ).limit(1)).first()
                if older_match is not None:
                    _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
                _error(404, "MATCH_EXPLANATION_MATCH_NOT_FOUND")

        if match["profile_version"] != profile["version"]:
            _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
        if not _number(match["score"]):
            _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")

        resolution = _current_resolution(search["criteria"])
        components = match["components"]
        if not isinstance(components, dict):
            _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
        saved_profile = components.get("scoring_profile")
        if saved_profile is not None and not isinstance(saved_profile, dict):
            _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
        if resolution.profile is None:
            if saved_profile is not None and saved_profile.get("selection") != "LEGACY_DEFAULT":
                _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
            scoring_profile = None
            resolution_label = "LEGACY_DEFAULT"
        else:
            selected = resolution.explanation
            if (not isinstance(saved_profile, dict)
                    or saved_profile.get("profile_id") != selected["profile_id"]
                    or saved_profile.get("profile_version") != selected["profile_version"]
                    or saved_profile.get("selected_role_family") != selected["selected_role_family"]
                    or saved_profile.get("selection") != selected["selection"]):
                _error(409, "MATCH_EXPLANATION_STALE_CONTEXT")
            scoring_profile = {
                "id": resolution.profile["id"],
                "version": resolution.profile["version"],
                "role_family": resolution.profile["role_family"],
            }
            resolution_label = "CURRENT"

        contributions = _stored_contributions(components, resolution)
        if resolution.profile is None and not contributions:
            contributions = [{
                "dimension": "skills",
                "weight": 100.0,
                "raw_score": match["score"],
                "weighted_contribution": match["score"],
                "explanation": (
                    "The saved deterministic match score is preserved because no scoring profile was selected."
                ),
                "provenance": {"source": "LEGACY_MATCH_BASELINE"},
            }]
        return {
            "profile_id": profile_id,
            "search_profile_id": search_profile_id,
            "job_id": job_id,
            "score": match["score"],
            "resolution": resolution_label,
            "profile_resolution": resolution.explanation,
            "scoring_profile": scoring_profile,
            "scoring_contributions": contributions,
        }

    return router
