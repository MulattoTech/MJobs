"""Profile-scoped API routes for reviewed, versioned career stories."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from services.career_profile.story_bank import (
    StoryBankError,
    create_story,
    list_stories,
    review_story,
    revise_story,
)


def _invoke(operation, *args, **kwargs):
    """Translate service outcomes to a stable, non-sensitive HTTP error shape."""
    try:
        return operation(*args, **kwargs)
    except StoryBankError as error:
        detail = {"code": error.code}
        if error.code in {"FACTS_UNAVAILABLE", "FACT_NOT_APPROVED", "FACT_FINGERPRINT_MISMATCH"}:
            detail["message"] = str(error)
        raise HTTPException(
            status_code=error.status_code,
            detail=detail,
        ) from None


def _request_object(payload, code: str) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(422, {"code": code})
    return payload


def create_career_story_bank_router(store) -> APIRouter:
    """Build story CRUD/review routes; approval remains explicit per revision."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/career-stories")
    def career_stories(profile_id: str):
        stories = _invoke(list_stories, store, profile_id)
        return {"profile_id": profile_id, "stories": stories}

    @router.post("/profiles/{profile_id}/career-stories", status_code=201)
    def create_career_story(profile_id: str, payload: object = Body(...)):
        body = _request_object(payload, "INVALID_STORY")
        return _invoke(create_story, store, profile_id, body)

    @router.post("/profiles/{profile_id}/career-stories/{story_id}/review")
    def review_career_story(profile_id: str, story_id: str,
                            payload: object = Body(...)):
        body = _request_object(payload, "INVALID_REVIEW")
        approved = body.get("approved")
        reviewer_note = body.get("reviewer_note", "")
        expected_revision = body.get("expected_revision")
        if type(expected_revision) is int and expected_revision < 1:
            # Keep ordinary review-body validation ahead of conflict mapping.
            if type(approved) is not bool:
                return _invoke(
                    review_story,
                    store,
                    profile_id,
                    story_id,
                    approved=approved,
                    reviewer_note=reviewer_note,
                    expected_revision=expected_revision,
                )
            if reviewer_note is not None and (
                    not isinstance(reviewer_note, str) or len(reviewer_note) > 2000):
                raise HTTPException(422, {"code": "INVALID_STORY"})
            raise HTTPException(409, {"code": "REVISION_CONFLICT"})
        return _invoke(
            review_story,
            store,
            profile_id,
            story_id,
            approved=approved,
            reviewer_note=reviewer_note,
            expected_revision=expected_revision,
        )

    @router.post("/profiles/{profile_id}/career-stories/{story_id}/versions",
                 status_code=201)
    def revise_career_story(profile_id: str, story_id: str,
                            payload: object = Body(...)):
        body = _request_object(payload, "INVALID_STORY")
        if "expected_revision" not in body:
            raise HTTPException(422, {"code": "REVISION_REQUIRED"})
        revision = body.get("expected_revision")
        story_payload = {key: value for key, value in body.items()
                         if key != "expected_revision"}
        return _invoke(
            revise_story,
            store,
            profile_id,
            story_id,
            story_payload,
            expected_revision=revision,
        )

    return router
