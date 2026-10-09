"""Read-only API previews for proposed wording of approved resume facts."""
from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import NoResultFound

from services.documents.errors import PreparationError
from services.documents.rewrite_review import build_rewrite_review
from services.documents.tailor import target_fingerprint


class ResumeRewriteProposal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    fact_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=10_000)


class ResumeRewriteReviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expected_profile_version: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
    )
    expected_job_fingerprint: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$",
    )
    selected_fact_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=100,
    )
    rewrites: list[ResumeRewriteProposal] = Field(min_length=1, max_length=100)


_RESPONSE_FIELDS = (
    "profile_id",
    "profile_version",
    "job_id",
    "job_fingerprint",
    "read_only",
    "review_required",
    "semantic_equivalence",
    "rewrites",
)
_REWRITE_FIELDS = {
    "fact_id",
    "fact_kind",
    "source_text",
    "source_sha256",
    "proposed_text",
    "protected_token_audit",
}
_AUDIT_FIELDS = {"required", "retained", "missing"}


def _load_profile(store, profile_id: str) -> dict:
    try:
        profile = store.profile(profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        raise HTTPException(
            404, {"code": "RESUME_REWRITE_PROFILE_NOT_FOUND"},
        ) from None
    if profile.get("id") != profile_id:
        raise HTTPException(
            404, {"code": "RESUME_REWRITE_PROFILE_NOT_FOUND"},
        )
    return profile


def _load_job(store, profile_id: str, job_id: str) -> dict:
    try:
        return store.job(job_id, profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        raise HTTPException(
            404, {"code": "RESUME_REWRITE_JOB_NOT_FOUND"},
        ) from None


def _validate_response(preview: dict, *, profile_id: str, profile_version: str,
                       job_id: str, job_fingerprint: str,
                       selected_fact_ids: list[str]) -> dict:
    if not isinstance(preview, dict) or any(field not in preview for field in _RESPONSE_FIELDS):
        raise HTTPException(500, {"code": "RESUME_REWRITE_PREVIEW_INVALID"})
    if (preview.get("profile_id") != profile_id
            or preview.get("profile_version") != profile_version
            or preview.get("job_id") != job_id
            or preview.get("job_fingerprint") != job_fingerprint
            or preview.get("read_only") is not True
            or preview.get("review_required") is not True
            or preview.get("semantic_equivalence") != "USER_REVIEW_REQUIRED"):
        raise HTTPException(500, {"code": "RESUME_REWRITE_PREVIEW_INVALID"})
    rows = preview.get("rewrites")
    if type(rows) is not list or len(rows) != len(selected_fact_ids):
        raise HTTPException(500, {"code": "RESUME_REWRITE_PREVIEW_INVALID"})
    for fact_id, row in zip(selected_fact_ids, rows):
        if (type(row) is not dict or set(row) != _REWRITE_FIELDS
                or row.get("fact_id") != fact_id
                or type(row.get("fact_kind")) is not str
                or type(row.get("source_text")) is not str
                or type(row.get("source_sha256")) is not str
                or len(row["source_sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in row["source_sha256"])
                or row["source_sha256"] != hashlib.sha256(
                    row["source_text"].encode("utf-8")).hexdigest()
                or type(row.get("proposed_text")) is not str
                or type(row.get("protected_token_audit")) is not dict
                or set(row["protected_token_audit"]) != _AUDIT_FIELDS
                or any(type(row["protected_token_audit"][key]) is not list
                       for key in _AUDIT_FIELDS)):
            raise HTTPException(500, {"code": "RESUME_REWRITE_PREVIEW_INVALID"})
        for key in _AUDIT_FIELDS:
            if any(type(token) is not dict or set(token) != {"type", "value"}
                   or type(token["type"]) is not str or type(token["value"]) is not str
                   for token in row["protected_token_audit"][key]):
                raise HTTPException(500, {"code": "RESUME_REWRITE_PREVIEW_INVALID"})
    return {field: preview[field] for field in _RESPONSE_FIELDS}


def create_resume_rewrite_review_router(store) -> APIRouter:
    """Bind a user proposal to current approved source facts without saving it."""
    router = APIRouter()

    @router.post(
        "/profiles/{profile_id}/jobs/{job_id}/resume-rewrite-review-preview",
    )
    def resume_rewrite_review_preview(
        profile_id: str,
        job_id: str,
        value: ResumeRewriteReviewInput,
    ):
        profile = _load_profile(store, profile_id)
        job = _load_job(store, profile_id, job_id)
        try:
            preview = build_rewrite_review(
                profile,
                job,
                value.selected_fact_ids,
                [item.model_dump() for item in value.rewrites],
                value.expected_profile_version,
                value.expected_job_fingerprint,
            )
        except PreparationError as error:
            status = 409 if error.code in {
                "RESUME_REWRITE_PROFILE_STALE", "RESUME_REWRITE_JOB_STALE",
            } else 422
            raise HTTPException(status, {"code": error.code}) from None

        current_profile = _load_profile(store, profile_id)
        current_job = _load_job(store, profile_id, job_id)
        try:
            current_job_fingerprint = target_fingerprint(current_job)
        except (KeyError, TypeError, ValueError):
            current_job_fingerprint = None
        if (current_profile["version"] != value.expected_profile_version
                or current_job_fingerprint != value.expected_job_fingerprint):
            code = (
                "RESUME_REWRITE_PROFILE_STALE"
                if current_profile["version"] != value.expected_profile_version
                else "RESUME_REWRITE_JOB_STALE"
            )
            raise HTTPException(409, {"code": code})

        return _validate_response(
            preview,
            profile_id=profile_id,
            profile_version=value.expected_profile_version,
            job_id=job_id,
            job_fingerprint=value.expected_job_fingerprint,
            selected_fact_ids=value.selected_fact_ids,
        )

    return router
