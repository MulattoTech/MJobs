"""Profile- and job-scoped API for persisted interview preparation packs."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from services.career_profile.interview_preparation import (
    InterviewPreparationError,
    create_interview_prep_pack,
    get_interview_prep_pack,
    list_interview_prep_packs,
)


_MAX_SELECTED_STORIES = 50


def _validate_selection_payload(payload: object) -> list[str]:
    if not isinstance(payload, dict) or set(payload) != {"selected_story_revision_ids"}:
        raise HTTPException(422, {"code": "INTERVIEW_PREPARATION_INVALID_REQUEST"})
    story_ids = payload["selected_story_revision_ids"]
    if not isinstance(story_ids, list) or len(story_ids) > _MAX_SELECTED_STORIES:
        raise HTTPException(422, {"code": "INTERVIEW_PREPARATION_INVALID_STORY_SELECTION"})
    if any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in story_ids):
        raise HTTPException(422, {"code": "INTERVIEW_PREPARATION_INVALID_STORY_SELECTION"})
    normalized = [value.strip() for value in story_ids]
    if len(set(normalized)) != len(normalized):
        raise HTTPException(422, {"code": "INTERVIEW_PREPARATION_INVALID_STORY_SELECTION"})
    return normalized


def _service_error(error: InterviewPreparationError) -> HTTPException:
    return HTTPException(
        error.status_code,
        {"code": f"INTERVIEW_PREPARATION_{error.code}"},
    )


def create_interview_preparation_router(store) -> APIRouter:
    """Mount deterministic create/list/detail operations for stored prep packs."""
    router = APIRouter()

    @router.post("/profiles/{profile_id}/jobs/{job_id}/interview-prep-packs")
    def create_pack(profile_id: str, job_id: str, payload: object = Body(...)):
        selected_ids = _validate_selection_payload(payload)
        try:
            return create_interview_prep_pack(store, profile_id, job_id, selected_ids)
        except InterviewPreparationError as error:
            raise _service_error(error) from None

    @router.get("/profiles/{profile_id}/jobs/{job_id}/interview-prep-packs")
    def list_packs(profile_id: str, job_id: str):
        try:
            packs = list_interview_prep_packs(store, profile_id, job_id)
        except InterviewPreparationError as error:
            raise _service_error(error) from None
        return {
            "schema_version": 1,
            "profile_id": profile_id,
            "job_id": job_id,
            "packs": packs,
        }

    @router.get("/profiles/{profile_id}/jobs/{job_id}/interview-prep-packs/{pack_id}")
    def get_pack(profile_id: str, job_id: str, pack_id: str):
        try:
            pack = get_interview_prep_pack(store, profile_id, job_id, pack_id)
        except InterviewPreparationError as error:
            raise _service_error(error) from None
        if pack is None:
            raise HTTPException(404, {"code": "INTERVIEW_PREPARATION_PACK_NOT_FOUND"})
        return pack

    return router
