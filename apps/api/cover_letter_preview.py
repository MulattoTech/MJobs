"""Read-only profile/job-scoped cover-letter preview endpoint."""
from __future__ import annotations

from typing import Literal
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.documents.cover_letter_preview import build_cover_letter_preview
from services.documents.errors import PreparationError
from services.documents.reviewed_cover_letter import (
    ReviewedCoverLetterError, materialize_reviewed_preview,
)


_ERROR_STATUS = {
    "COVER_LETTER_PREVIEW_PROFILE_REQUIRED": 422,
    "COVER_LETTER_PREVIEW_PROFILE_STALE": 409,
    "COVER_LETTER_PREVIEW_JOB_REQUIRED": 422,
    "COVER_LETTER_PREVIEW_JOB_STALE": 409,
    "COVER_LETTER_PREVIEW_EVIDENCE_INVALID": 422,
    "COVER_LETTER_OPTIONS_INVALID": 422,
    "COVER_LETTER_JOB_CONTEXT_MISSING": 422,
    "COVER_LETTER_RELEVANT_FACTS_MISSING": 422,
}


class CoverLetterPreviewRequest(BaseModel):
    expected_profile_version: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    expected_job_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    style: Literal["professional", "warm", "technical", "direct", "conversational", "formal"] = "professional"
    length: Literal["very short", "concise", "normal", "detailed"] = "normal"


class CoverLetterReviewRequest(CoverLetterPreviewRequest):
    expected_base_document_version: int = Field(ge=1)
    preview_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _profile(store, profile_id: str) -> dict:
    try:
        profile = store.profile(profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        raise HTTPException(404, {"code": "COVER_LETTER_PREVIEW_PROFILE_NOT_FOUND"}) from None
    if not isinstance(profile, dict) or profile.get("id") != profile_id:
        raise HTTPException(404, {"code": "COVER_LETTER_PREVIEW_PROFILE_NOT_FOUND"})
    return profile


def _job(store, job_id: str) -> dict:
    with store.engine.connect() as connection:
        row = connection.execute(
            select(m.jobs, m.companies.c.name.label("company")).select_from(
                m.jobs.join(m.companies, m.jobs.c.company_id == m.companies.c.id)
            ).where(m.jobs.c.id == job_id),
        ).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, {"code": "COVER_LETTER_PREVIEW_JOB_NOT_FOUND"})
    return dict(row)


def create_cover_letter_preview_router(store, output_dir: str | Path) -> APIRouter:
    """Build a preview exclusively from current local saved snapshots."""
    router = APIRouter()

    @router.post("/profiles/{profile_id}/jobs/{job_id}/cover-letter-preview")
    def cover_letter_preview(profile_id: str, job_id: str, request: CoverLetterPreviewRequest):
        profile = _profile(store, profile_id)
        job = _job(store, job_id)
        try:
            preview = build_cover_letter_preview(
                profile,
                job,
                profile.get("facts"),
                expected_profile_version=request.expected_profile_version,
                expected_job_fingerprint=request.expected_job_fingerprint,
                style=request.style,
                length=request.length,
            )
        except PreparationError as error:
            status = _ERROR_STATUS.get(error.code, 422)
            raise HTTPException(status, {"code": error.code}) from None

        # Re-read after rendering to avoid returning a preview whose source rows
        # changed while it was being assembled. The helper validates the same
        # exact inputs; this closes the route-level snapshot race.
        current_profile = _profile(store, profile_id)
        current_job = _job(store, job_id)
        try:
            from services.documents.cover_letter_preview import cover_letter_preview_fingerprint
            current_job_fingerprint = cover_letter_preview_fingerprint(current_job)
        except (KeyError, TypeError, ValueError):
            raise HTTPException(409, {"code": "COVER_LETTER_PREVIEW_JOB_STALE"}) from None
        if (current_profile.get("version") != profile.get("version")
                or current_job_fingerprint != preview["job_fingerprint"]):
            raise HTTPException(409, {"code": "COVER_LETTER_PREVIEW_CONTEXT_STALE"})
        if (preview.get("profile_id") != profile_id or preview.get("job_id") != job_id
                or preview.get("read_only") is not True or preview.get("document_created") is not False):
            raise HTTPException(422, {"code": "COVER_LETTER_PREVIEW_RESULT_INVALID"})
        return preview

    @router.post(
        "/profiles/{profile_id}/applications/{application_id}/cover-letter-reviews",
        status_code=201,
    )
    def approve_cover_letter(
        profile_id: str, application_id: str, request: CoverLetterReviewRequest,
    ):
        profile = _profile(store, profile_id)
        try:
            application = store.application(application_id)
        except (NoResultFound, KeyError, TypeError, ValueError):
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None
        if application.get("profile_id") != profile_id:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"})
        job = _job(store, application["job_id"])
        try:
            preview = build_cover_letter_preview(
                profile,
                job,
                profile.get("facts"),
                expected_profile_version=request.expected_profile_version,
                expected_job_fingerprint=request.expected_job_fingerprint,
                style=request.style,
                length=request.length,
            )
            return materialize_reviewed_preview(
                store,
                output_dir,
                profile=profile,
                job=job,
                application_id=application_id,
                expected_base_document_version=request.expected_base_document_version,
                preview=preview,
                preview_sha256=request.preview_sha256,
                request_id=request.request_id,
            )
        except PreparationError as error:
            status = _ERROR_STATUS.get(error.code, 422)
            raise HTTPException(status, {"code": error.code}) from None
        except ReviewedCoverLetterError as error:
            status = 409 if error.code.endswith(("_STALE", "_CONFLICT")) else 422
            raise HTTPException(status, {"code": error.code}) from None

    return router
