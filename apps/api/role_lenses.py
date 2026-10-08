"""Read-only profile/job-scoped role-lens catalog and evidence preview."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.career_profile.role_lenses import (
    RoleLensError,
    list_role_lenses,
    select_role_lens,
)
from services.documents.tailor import target_fingerprint


def _profile(store, profile_id: str) -> dict:
    try:
        profile = store.profile(profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        raise HTTPException(404, {"code": "ROLE_LENS_PROFILE_NOT_FOUND"}) from None
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        raise HTTPException(404, {"code": "ROLE_LENS_PROFILE_NOT_FOUND"})
    return profile


def _job(store, job_id: str) -> dict:
    with store.engine.connect() as connection:
        row = connection.execute(
            select(m.jobs).where(m.jobs.c.id == job_id),
        ).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, {"code": "ROLE_LENS_JOB_NOT_FOUND"})
    job = dict(row)
    try:
        fingerprint = target_fingerprint(job)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, {"code": "ROLE_LENS_JOB_UNAVAILABLE"}) from None
    return {"job": job, "fingerprint": fingerprint}


def create_role_lenses_router(store) -> APIRouter:
    """Expose current lens choices without writing profile or job state."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/jobs/{job_id}/role-lenses/selection")
    def role_lens_selection(
            profile_id: str,
            job_id: str,
            role_lens_id: str | None = Query(default=None, min_length=1, max_length=32),
            expected_role_lens_version: int | None = Query(default=None),
    ):
        if (role_lens_id is None) != (expected_role_lens_version is None):
            raise HTTPException(422, {"code": "ROLE_LENS_VERSION_INVALID"})
        profile = _profile(store, profile_id)
        job_snapshot = _job(store, job_id)
        try:
            selection = select_role_lens(
                store, profile_id, job_snapshot["job"].get("title"),
                role_lens_id=role_lens_id,
                expected_role_lens_version=expected_role_lens_version,
            )
        except RoleLensError as error:
            code = ("ROLE_LENS_PROFILE_NOT_FOUND" if error.code == "PROFILE_NOT_FOUND"
                    else error.code)
            raise HTTPException(error.status_code, {"code": code}) from None

        current_profile = _profile(store, profile_id)
        current_job = _job(store, job_id)
        if (current_profile["version"] != profile["version"]
                or current_job["fingerprint"] != job_snapshot["fingerprint"]
                or selection.get("profile_version") != profile["version"]):
            raise HTTPException(409, {"code": "ROLE_LENS_CONTEXT_STALE"})

        return {
            "schema_version": 1,
            "profile_id": profile_id,
            "profile_version": profile["version"],
            "job_id": job_id,
            "job_fingerprint": job_snapshot["fingerprint"],
            "role_lenses": list_role_lenses(),
            "selection": selection,
        }

    return router
