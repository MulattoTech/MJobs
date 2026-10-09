"""Read-only profile/job-scoped view of conservative recruiter classification."""
from __future__ import annotations

from math import isfinite
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m


_EVIDENCE_KEY = "recruiter_classification_evidence"
_CLASSIFICATIONS = {"DIRECT_EMPLOYER", "STAFFING_AGENCY", "RECRUITER", "UNKNOWN"}
_EVIDENCE_FIELDS = {
    "classification", "subject_type", "subject_id", "source", "evidence_ref",
    "current", "explicit", "confidence",
}


def _error(status_code: int, code: str):
    raise HTTPException(status_code=status_code, detail={"code": code})


def _profile(store, profile_id: str) -> dict:
    try:
        profile = store.profile(profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        _error(404, "RECRUITER_CLASSIFICATION_PROFILE_NOT_FOUND")
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        _error(404, "RECRUITER_CLASSIFICATION_PROFILE_NOT_FOUND")
    return profile


def _load_scoped_job(store, profile_id: str, profile_version: str, job_id: str) -> dict:
    with store.engine.connect() as connection:
        job = connection.execute(select(m.jobs).where(
            m.jobs.c.id == job_id,
        )).mappings().one_or_none()
        if job is None:
            _error(404, "RECRUITER_CLASSIFICATION_JOB_NOT_FOUND")

        current_match = connection.execute(select(m.matches.c.id).where(
            m.matches.c.profile_id == profile_id,
            m.matches.c.profile_version == profile_version,
            m.matches.c.job_id == job_id,
        ).limit(1)).first()
        if current_match is None:
            stale_match = connection.execute(select(m.matches.c.id).where(
                m.matches.c.profile_id == profile_id,
                m.matches.c.job_id == job_id,
            ).limit(1)).first()
            if stale_match is not None:
                _error(409, "RECRUITER_CLASSIFICATION_STALE_CONTEXT")
            _error(404, "RECRUITER_CLASSIFICATION_JOB_NOT_FOUND")
    return dict(job)


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "classification": "UNKNOWN",
        "confidence": None,
        "reasons": [reason],
        "evidence": [],
        "unknowns": ["recruiter_classification"],
        "advisory": True,
    }


def _valid_evidence_item(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _EVIDENCE_FIELDS:
        return False
    if value.get("classification") not in _CLASSIFICATIONS:
        return False
    if value.get("subject_type") not in {"JOB", "COMPANY"}:
        return False
    for field, limit in (("subject_id", 128), ("source", 200), ("evidence_ref", 2000)):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip() or len(item) > limit:
            return False
    confidence = value.get("confidence")
    return (
        type(value.get("current")) is bool
        and type(value.get("explicit")) is bool
        and type(confidence) in (int, float)
        and isfinite(confidence)
        and 0 <= confidence <= 1
    )


def _normalize_result(value: Any, *, expected_job_id: str,
                      expected_company_id: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("classification result must be an object")
    classification = value.get("classification")
    reasons = value.get("reasons")
    evidence = value.get("evidence")
    unknowns = value.get("unknowns")
    if (classification not in _CLASSIFICATIONS
            or not isinstance(reasons, list) or len(reasons) > 50
            or any(not isinstance(reason, str) or not reason.strip() or len(reason) > 1000
                   for reason in reasons)
            or not isinstance(evidence, list) or len(evidence) > 100
            or any(not _valid_evidence_item(item) for item in evidence)
            or not isinstance(unknowns, list) or len(unknowns) > 50
            or any(not isinstance(item, str) or not item.strip() or len(item) > 128
                   for item in unknowns)):
        raise ValueError("classification result is malformed")

    confidence = value.get("confidence")
    if classification == "UNKNOWN":
        if confidence not in (None, 0, 0.0):
            raise ValueError("unknown classification cannot assert confidence")
        confidence = None
    elif (type(confidence) not in (int, float) or not isfinite(confidence)
          or not 0 <= confidence <= 1 or not evidence):
        raise ValueError("classified result lacks trustworthy evidence")

    # This endpoint is a view; it never translates an advisory label into policy.
    if value.get("advisory", True) is not True:
        raise ValueError("classification result must remain advisory")
    for item in evidence:
        expected_subject = (expected_job_id if item["subject_type"] == "JOB"
                            else expected_company_id)
        if (expected_subject is None or item["subject_id"] != expected_subject
                or (classification != "UNKNOWN"
                    and (item["classification"] != classification
                         or item["current"] is not True
                         or item["explicit"] is not True))):
            raise ValueError("classification result contains out-of-scope evidence")
    return {
        "classification": classification,
        "confidence": float(confidence) if confidence is not None else None,
        "reasons": list(reasons),
        "evidence": [dict(item) for item in evidence],
        "unknowns": list(unknowns),
        "advisory": True,
    }


def _default_classifier(evidence, *, expected_job_id, expected_company_id):
    # The dependency is imported here so the API module stays importable in isolated
    # tests that inject the same callable contract.
    from services.discovery.recruiter_classification import classify_recruiter

    return classify_recruiter(
        evidence,
        expected_job_id=expected_job_id,
        expected_company_id=expected_company_id,
    )


def create_recruiter_classification_router(
        store, *, classifier: Callable[..., dict] | None = None) -> APIRouter:
    """Expose current recruiter classification without changing match or policy state."""
    router = APIRouter()
    run_classifier = classifier or _default_classifier

    @router.get("/profiles/{profile_id}/jobs/{job_id}/recruiter-classification")
    def recruiter_classification(profile_id: str, job_id: str):
        profile = _profile(store, profile_id)
        job = _load_scoped_job(store, profile_id, profile["version"], job_id)
        attributes = job.get("attributes")
        evidence = attributes.get(_EVIDENCE_KEY) if isinstance(attributes, dict) else None
        if evidence is None:
            evidence = []
        try:
            result = _normalize_result(run_classifier(
                evidence,
                expected_job_id=job_id,
                expected_company_id=job.get("company_id"),
            ), expected_job_id=job_id, expected_company_id=job.get("company_id"))
        except (TypeError, ValueError):
            result = _unknown(
                "Current recruiter classification evidence is malformed or unavailable."
            )
        return {"profile_id": profile_id, "job_id": job_id, **result}

    return router
