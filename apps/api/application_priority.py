"""Read-only profile/job-scoped projection of advisory application priority."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.discovery.recruiter_classification import classify_recruiter
from services.matching.user_match_feedback import list_feedback_preferences


_FACTOR_FIELDS = {"key", "label", "score", "contribution", "status", "explanation", "source"}
_PRIORITY_BANDS = {"UNKNOWN", "LOW", "NORMAL", "HIGH"}


def _error(status_code: int, code: str):
    raise HTTPException(status_code=status_code, detail={"code": code})


def _profile(store, profile_id: str) -> dict:
    try:
        profile = store.profile(profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        _error(404, "APPLICATION_PRIORITY_PROFILE_NOT_FOUND")
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        _error(404, "APPLICATION_PRIORITY_PROFILE_NOT_FOUND")
    return profile


def _load_job_match(store, profile_id: str, profile_version: str, job_id: str):
    with store.engine.connect() as connection:
        job = connection.execute(select(m.jobs).where(
            m.jobs.c.id == job_id,
        )).mappings().one_or_none()
        if job is None:
            _error(404, "APPLICATION_PRIORITY_JOB_NOT_FOUND")
        match = connection.execute(select(m.matches).where(
            m.matches.c.job_id == job_id,
            m.matches.c.profile_id == profile_id,
            m.matches.c.profile_version == profile_version,
        )).mappings().one_or_none()
        if match is None:
            prior_match = connection.execute(select(m.matches.c.id).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == profile_id,
            ).limit(1)).first()
            if prior_match is not None:
                _error(409, "APPLICATION_PRIORITY_STALE_CONTEXT")
            _error(404, "APPLICATION_PRIORITY_JOB_NOT_FOUND")

        posting_times = connection.execute(select(m.postings.c.fetched_at).where(
            m.postings.c.job_id == job_id,
        ).order_by(m.postings.c.fetched_at.desc(), m.postings.c.id.asc())).scalars().all()
    return dict(job), dict(match), list(posting_times)


def _salary_status(components: Any):
    if not isinstance(components, Mapping):
        return None
    factors = components.get("fit_factors")
    if not isinstance(factors, list):
        return None
    matches = [row for row in factors if isinstance(row, Mapping) and row.get("id") == "salary"]
    if len(matches) != 1:
        return None
    return matches[0].get("status")


def _fit_factor_status(components: Any, factor_id: str):
    if not isinstance(components, Mapping):
        return None
    factors = components.get("fit_factors")
    if not isinstance(factors, list):
        return None
    matches = [row for row in factors if isinstance(row, Mapping) and row.get("id") == factor_id]
    if len(matches) != 1:
        return None
    status = matches[0].get("status")
    return status if status in {"ALIGNS", "NO_DIRECT_MATCH"} else None


def _latest_source_freshness(values: list[Any]):
    # Rows are sorted newest-first by their persisted fetch timestamp. Leave a
    # malformed explicit value for the scorer to report as UNKNOWN.
    return values[0] if values else None


def _signals(job: Mapping[str, Any], match: Mapping[str, Any],
             posting_times: list[Any], job_id: str,
             applied_preferences: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    signals: dict[str, Any] = {"match_score": match.get("score")}
    attributes = job.get("attributes")
    if not isinstance(attributes, Mapping):
        attributes = {}

    if "reported_posted_at" in attributes:
        signals["posted_at"] = attributes["reported_posted_at"]
    salary_status = _salary_status(match.get("components"))
    if salary_status is not None:
        signals["salary_status"] = salary_status
    work_arrangement_status = _fit_factor_status(match.get("components"), "work_arrangement")
    if work_arrangement_status is not None:
        signals["work_arrangement_status"] = work_arrangement_status
    company_id = job.get("company_id")
    company_feedback = [
        item for item in (applied_preferences or [])
        if item.get("dimension") == "company"
        and item.get("company_id") == company_id
        and item.get("category") in {"GOOD_MATCH", "BAD_COMPANY"}
    ]
    if company_feedback:
        latest_company_feedback = max(
            company_feedback,
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        )
        signals["company_preference"] = (
            "PREFERRED" if latest_company_feedback["category"] == "GOOD_MATCH" else "DISFAVORED"
        )
    if "application_complexity" in attributes:
        signals["application_complexity"] = attributes["application_complexity"]
    if "expires_at" in attributes:
        signals["expires_at"] = attributes["expires_at"]

    if posting_times:
        signals["source_freshness_at"] = _latest_source_freshness(posting_times)

    # Classify only structured, entity-bound evidence already stored with the job.
    # Job prose, domains, source names, and ATS identities are never interpreted.
    if "recruiter_classification_evidence" in attributes:
        classification = classify_recruiter(
            attributes["recruiter_classification_evidence"],
            expected_job_id=job_id,
            expected_company_id=job.get("company_id"),
        )
        signals["direct_employer_availability"] = classification["classification"]
    return signals


def rank_jobs_by_application_priority(store, profile_id: str, jobs: list[dict[str, Any]], *,
                                       now: datetime | None = None) -> list[dict[str, Any]]:
    """Return a bounded search result ordered by explainable advisory priority.

    This adds display-only priority values to the supplied result rows. It never
    changes persisted match scores, saved-search filters, or application state.
    """
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("APPLICATION_PRIORITY_PROFILE_REQUIRED")
    if not isinstance(jobs, list) or any(not isinstance(row, dict) for row in jobs):
        raise ValueError("APPLICATION_PRIORITY_JOBS_INVALID")
    applied = list_feedback_preferences(store, profile_id)["items"]
    job_ids = [row.get("id") for row in jobs]
    if any(not isinstance(job_id, str) or not job_id for job_id in job_ids):
        raise ValueError("APPLICATION_PRIORITY_JOBS_INVALID")
    posting_times: dict[str, list[Any]] = {job_id: [] for job_id in job_ids}
    if job_ids:
        with store.engine.connect() as connection:
            observed = connection.execute(select(
                m.postings.c.job_id, m.postings.c.fetched_at,
            ).where(m.postings.c.job_id.in_(job_ids)).order_by(
                m.postings.c.job_id, m.postings.c.fetched_at.desc(), m.postings.c.id,
            )).all()
        for job_id, fetched_at in observed:
            posting_times[job_id].append(fetched_at)

    from services.discovery.application_priority import explain_application_priority

    observed_now = now or datetime.now(timezone.utc)
    for row in jobs:
        match = {"score": row.get("match_score"), "components": row.get("match_components")}
        result = explain_application_priority(
            _signals(row, match, posting_times[row["id"]], row["id"], applied),
            now=observed_now,
            applied_preferences=applied,
        )
        score = result["priority_score"]
        row["application_priority_score"] = score
        row["application_priority_band"] = result["priority_band"]

    def order(row):
        score = row["application_priority_score"]
        match_score = row.get("match_score")
        return (
            score is None,
            -(score if score is not None else 0.0),
            match_score is None,
            -(match_score if isinstance(match_score, (int, float)) else 0.0),
            str(row.get("title") or "").casefold(),
            row["id"],
        )

    return sorted(jobs, key=order)


def _number(value: Any, *, nullable=False) -> bool:
    if nullable and value is None:
        return True
    return type(value) in (int, float) and isfinite(value) and 0 <= value <= 100


def _normalize_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("priority result must be an object")
    score = value.get("priority_score")
    band = value.get("priority_band")
    coverage = value.get("coverage_percent")
    factors = value.get("factors")
    unknowns = value.get("unknowns")
    if (not _number(score, nullable=True)
            or band not in _PRIORITY_BANDS
            or not _number(coverage)
            or not isinstance(factors, list) or len(factors) > 32
            or not isinstance(unknowns, list) or len(unknowns) > 32):
        raise ValueError("priority result summary is invalid")

    normalized_factors = []
    seen = set()
    for factor in factors:
        if (not isinstance(factor, Mapping) or set(factor) != _FACTOR_FIELDS
                or not isinstance(factor.get("key"), str) or not factor["key"].strip()
                or len(factor["key"]) > 100 or factor["key"] in seen
                or not isinstance(factor.get("label"), str) or not factor["label"].strip()
                or len(factor["label"]) > 200
                or factor.get("status") not in {"KNOWN", "UNKNOWN"}
                or not isinstance(factor.get("explanation"), str)
                or not factor["explanation"].strip() or len(factor["explanation"]) > 2000
                or not isinstance(factor.get("source"), str)
                or not factor["source"].strip() or len(factor["source"]) > 200):
            raise ValueError("priority factor is invalid")
        row_score, contribution = factor["score"], factor["contribution"]
        if factor["status"] == "UNKNOWN":
            if row_score is not None or contribution is not None:
                raise ValueError("unknown factor cannot claim a score or contribution")
        elif not _number(row_score) or not _number(contribution):
            raise ValueError("known factor needs a finite score and contribution")
        normalized_factors.append({key: factor[key] for key in _FACTOR_FIELDS})
        seen.add(factor["key"])

    expected_unknowns = [row["key"] for row in normalized_factors if row["status"] == "UNKNOWN"]
    if unknowns != expected_unknowns:
        raise ValueError("priority unknown list does not match its factors")
    if value.get("advisory") is not True or value.get("affects_match_score") is not False:
        raise ValueError("priority result must remain advisory and score-independent")
    if (score is None) != (band == "UNKNOWN"):
        raise ValueError("priority score and band disagree")
    return {
        "priority_score": float(score) if score is not None else None,
        "priority_band": band,
        "coverage_percent": coverage,
        "factors": normalized_factors,
        "unknowns": list(unknowns),
        "advisory": True,
        "affects_match_score": False,
    }


def create_application_priority_router(
        store, *, now_provider: Callable[[], datetime] | None = None,
        explainer: Callable[..., Mapping[str, Any]] | None = None) -> APIRouter:
    """Expose deterministic priority context without writes or eligibility actions."""
    router = APIRouter()
    run_explainer = explainer
    if run_explainer is None:
        from services.discovery.application_priority import explain_application_priority

        run_explainer = explain_application_priority
    clock = now_provider or (lambda: datetime.now(timezone.utc))

    @router.get("/profiles/{profile_id}/jobs/{job_id}/application-priority")
    def application_priority(profile_id: str, job_id: str):
        profile = _profile(store, profile_id)
        job, match, posting_times = _load_job_match(
            store, profile_id, profile["version"], job_id,
        )
        match_score = match.get("score")
        if not _number(match_score):
            _error(409, "APPLICATION_PRIORITY_STALE_CONTEXT")
        try:
            reviewed = list_feedback_preferences(store, profile_id)["items"]
            signals = _signals(job, match, posting_times, job_id, reviewed)
            priority = _normalize_result(run_explainer(
                signals, now=clock(), applied_preferences=reviewed,
            ))
        except (TypeError, ValueError):
            _error(502, "APPLICATION_PRIORITY_RESULT_INVALID")
        return {
            "profile_id": profile_id,
            "job_id": job_id,
            "match_score": float(match_score),
            **priority,
        }

    return router
