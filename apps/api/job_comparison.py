"""Read-only comparison of explicitly selected, currently scored jobs."""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from importlib import import_module
from math import isfinite
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.discovery.search_semantics import filter_jobs
from services.discovery.skill_taxonomy import canonical_skill_id, skill_label
from services.normalization.jobs import canonical_job_view


class ComparisonService(Protocol):
    def __call__(
        self,
        *,
        profile_id: str,
        search_profile_id: str,
        job_ids: Sequence[str],
        snapshots: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


def _error(status_code: int, code: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code})


class JobComparisonRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    job_ids: list[str] = Field(min_length=2, max_length=10)

    @field_validator("job_ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or value != value.strip() or len(value) > 128 for value in values):
            raise ValueError("job_ids must contain bounded, nonblank, trimmed identifiers")
        if len(set(values)) != len(values):
            raise ValueError("job_ids must be unique")
        return values


def _comparison_service():
    """Load Kanan's pure comparison contract at request time for integration-safe mounting."""
    try:
        return import_module("services.matching.job_comparison").compare_scored_jobs
    except (ImportError, AttributeError) as exc:
        raise HTTPException(status_code=503, detail={"code": "JOB_COMPARISON_SERVICE_UNAVAILABLE"}) from exc


def _scoring_snapshot(search_criteria: Any, match: Mapping[str, Any]) -> list[dict]:
    """Project and validate immutable contribution provenance for a saved match."""
    from apps.api.matching_explanations import (
        _current_resolution,
        _stored_contributions,
        _validate_contribution_score,
    )

    score = match["score"]
    if type(score) not in (int, float) or not isfinite(score) or not 0 <= score <= 100:
        _error(409, "JOB_COMPARISON_STALE_CONTEXT")
    components = match["components"]
    if not isinstance(components, dict):
        _error(409, "JOB_COMPARISON_STALE_CONTEXT")
    resolution = _current_resolution(search_criteria)
    saved_profile = components.get("scoring_profile")
    if saved_profile is not None and not isinstance(saved_profile, dict):
        _error(409, "JOB_COMPARISON_STALE_CONTEXT")
    if resolution.profile is None:
        if saved_profile is not None and saved_profile.get("selection") != "LEGACY_DEFAULT":
            _error(409, "JOB_COMPARISON_STALE_CONTEXT")
        contributions = _stored_contributions(components, resolution)
        if not contributions:
            contributions = [{
                "dimension": "skills", "weight": 100.0, "raw_score": score,
                "weighted_contribution": score,
                "explanation": "The saved deterministic match score is preserved because no scoring profile was selected.",
                "provenance": {"source": "LEGACY_MATCH_BASELINE"},
            }]
    else:
        expected = resolution.explanation
        if (not isinstance(saved_profile, dict)
                or saved_profile.get("profile_id") != expected["profile_id"]
                or saved_profile.get("profile_version") != expected["profile_version"]
                or saved_profile.get("selected_role_family") != expected["selected_role_family"]
                or saved_profile.get("selection") != expected["selection"]):
            _error(409, "JOB_COMPARISON_STALE_CONTEXT")
        contributions = _stored_contributions(components, resolution)
    _validate_contribution_score(score, contributions, resolution)
    return contributions


def _required_requirements(attributes: Any) -> list[dict] | None:
    """Return only explicit, provenance-backed, structured required terms.

    Description mentions and untrusted/missing provenance never become comparison
    evidence. The pure service validates malformed normalized terms and preserves
    their unknown state.
    """
    if not isinstance(attributes, dict):
        return None
    if attributes.get("skill_evidence") not in {
        "structured_requirements", "structured_required_skills",
    }:
        return None
    required = attributes.get("required_skills")
    source = attributes.get("source_type")
    if (not isinstance(required, list) or not isinstance(source, str)
            or not source.strip()):
        return None
    evidence_ref = attributes.get("requirement_source_ref", attributes.get("source_ref"))
    if evidence_ref is not None and (not isinstance(evidence_ref, str) or not evidence_ref.strip()):
        return None

    provenance = {"source": source.strip()}
    if evidence_ref is not None:
        provenance["evidence_ref"] = evidence_ref.strip()
    terms = []
    for value in required:
        # Stored matching attributes have historically held taxonomy labels. Convert
        # only those explicit values to their canonical normalized labels; unknown
        # values remain verbatim for the service to report as invalid stored evidence.
        normalized_id = canonical_skill_id(value) if isinstance(value, str) else None
        normalized = skill_label(normalized_id).casefold() if normalized_id else None
        terms.append({
            "normalized_term": normalized if normalized is not None else value,
            "provenance": dict(provenance),
        })
    return terms


def _saved_filter_members(store, profile_id, job_ids, criteria):
    """Apply saved list filters to only the selected IDs, without page truncation."""
    from services.discovery.search_profiles import _clean_job_filters

    try:
        filters = _clean_job_filters(criteria.get("job_filters"))
    except (TypeError, ValueError):
        _error(409, "JOB_COMPARISON_STALE_CONTEXT")
    if filters is None:
        return set(job_ids)
    kwargs = {key: filters.get(key) or None for key in (
        "remote_type", "provider", "experience_level", "employment_type",
        "education_requirement", "clearance_level", "job_source_id", "ats_provider",
        "application_link_status", "application_pipeline_status", "application_capability",
    )}
    kwargs.update({key: filters.get(key, "") for key in ("q", "title", "company", "location")})
    for key in ("salary_min", "salary_max"):
        kwargs[key] = float(filters[key]) if filters.get(key) else None
    kwargs["min_match_score"] = int(filters["min_match_score"]) if filters.get("min_match_score") else None
    kwargs["skill_id"] = canonical_skill_id(filters["skill"]) if filters.get("skill") else None
    if filters.get("posted_within_days"):
        kwargs["posted_before"] = datetime.now(timezone.utc)
        kwargs["posted_after"] = kwargs["posted_before"] - timedelta(days=int(filters["posted_within_days"]))
    return {row["id"] for row in store.jobs(
        profile_id=profile_id, job_ids=job_ids, limit=len(job_ids), **kwargs,
    )}


def create_job_comparison_router(
    store,
    compare_scored_jobs: ComparisonService | None = None,
) -> APIRouter:
    """Create a read-only endpoint over current persisted matches for one saved search."""
    router = APIRouter()

    @router.post(
        "/profiles/{profile_id}/search-profiles/{search_profile_id}/jobs/compare"
    )
    def compare_jobs(profile_id: str, search_profile_id: str, request: JobComparisonRequest):
        try:
            profile = store.profile(profile_id)
        except NoResultFound:
            _error(404, "JOB_COMPARISON_SCOPE_MISMATCH")

        with store.engine.connect() as connection:
            search = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == search_profile_id,
                m.search_profiles.c.profile_id == profile_id,
            )).mappings().one_or_none()
            if search is None:
                _error(409, "JOB_COMPARISON_SCOPE_MISMATCH")

            jobs = connection.execute(select(m.jobs, m.companies.c.name.label("company")).join(
                m.companies, m.companies.c.id == m.jobs.c.company_id,
            ).where(m.jobs.c.id.in_(request.job_ids))).mappings().all()
            by_job = {row["id"]: row for row in jobs}
            if set(by_job) != set(request.job_ids):
                _error(409, "JOB_COMPARISON_SCOPE_MISMATCH")
            # Use the same normalized search semantics as the jobs list. Search
            # ownership and a current score alone do not establish membership.
            eligible = filter_jobs(
                [canonical_job_view(dict(row)) for row in jobs], search["criteria"],
            )
            if {row["id"] for row in eligible} != set(request.job_ids):
                _error(409, "JOB_COMPARISON_SCOPE_MISMATCH")

            match_rows = connection.execute(select(m.matches).where(
                m.matches.c.profile_id == profile_id,
                m.matches.c.job_id.in_(request.job_ids),
            )).mappings().all()
            matches_by_job = {}
            for match in match_rows:
                matches_by_job.setdefault(match["job_id"], []).append(match)

            snapshots = []
            for job_id in request.job_ids:
                matches = matches_by_job.get(job_id, [])
                if not matches:
                    _error(409, "JOB_COMPARISON_SCOPE_MISMATCH")
                current = [row for row in matches if row["profile_version"] == profile["version"]]
                if len(current) != 1:
                    if matches:
                        _error(409, "JOB_COMPARISON_STALE_CONTEXT")
                    _error(409, "JOB_COMPARISON_SCOPE_MISMATCH")
                match = current[0]
                try:
                    contributions = _scoring_snapshot(search["criteria"], match)
                except HTTPException as exc:
                    if exc.status_code == 409:
                        _error(409, "JOB_COMPARISON_STALE_CONTEXT")
                    raise

                job = by_job[job_id]
                snapshot = {
                    "job_id": job_id,
                    "title": job["title"],
                    "company": job["company"],
                    "match_score": match["score"],
                    "scoring_contributions": contributions,
                }
                requirements = _required_requirements(job["attributes"])
                if requirements is not None:
                    snapshot["required_requirements"] = requirements
                snapshots.append(snapshot)

        # Release the snapshot connection before the store's filtered query;
        # comparisons must not hold one pool slot while waiting for another.
        if _saved_filter_members(store, profile_id, request.job_ids, search["criteria"]) != set(request.job_ids):
            _error(409, "JOB_COMPARISON_SCOPE_MISMATCH")
        service = compare_scored_jobs or _comparison_service()
        result = service(
            profile_id=profile_id,
            search_profile_id=search_profile_id,
            job_ids=list(request.job_ids),
            snapshots=snapshots,
        )
        if not isinstance(result, Mapping):
            _error(503, "JOB_COMPARISON_SERVICE_UNAVAILABLE")
        return dict(result)

    return router
