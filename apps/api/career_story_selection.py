"""Read-only API adapter for profile-scoped, job-specific story choices."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.career_profile.story_selection import (
    StorySelectionError,
    select_stories_for_job,
)
from services.documents.tailor import target_fingerprint


_ALLOWED_USE_FILTERS = frozenset({"RESUME", "COVER_LETTER"})


def _profile(store, profile_id: str) -> dict:
    try:
        return store.profile(profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        raise HTTPException(
            404, {"code": "CAREER_STORY_SELECTION_PROFILE_NOT_FOUND"},
        ) from None


def _job(store, job_id: str) -> dict:
    with store.engine.connect() as connection:
        row = connection.execute(
            select(m.jobs).where(m.jobs.c.id == job_id),
        ).mappings().one_or_none()
    if row is None:
        raise HTTPException(
            404, {"code": "CAREER_STORY_SELECTION_JOB_NOT_FOUND"},
        )
    job = dict(row)
    try:
        fingerprint = target_fingerprint(job)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            409, {"code": "CAREER_STORY_SELECTION_JOB_UNAVAILABLE"},
        ) from None
    return {"job": job, "fingerprint": fingerprint}


def _selection_row(row: dict) -> dict:
    fact_ids = [value for value in row.get("fact_ids", []) if isinstance(value, str)]
    fingerprints = [
        {"fact_id": value["fact_id"], "fingerprint": value["fingerprint"]}
        for value in row.get("fact_fingerprints", [])
        if isinstance(value, dict)
        and isinstance(value.get("fact_id"), str)
        and isinstance(value.get("fingerprint"), str)
    ]
    return {
        "story_id": row.get("story_id"),
        "story_key": row.get("story_key"),
        "revision": row.get("revision"),
        "profile_version": row.get("profile_version"),
        "kind": row.get("kind"),
        "title": row.get("title"),
        # This is the exact stored, user-authored story text. No generation runs here.
        "text": row.get("text"),
        "fact_ids": fact_ids,
        "fact_fingerprints": fingerprints,
        "provenance": {
            "fact_ids": fact_ids,
            "fact_fingerprints": fingerprints,
        },
        "tags": list(row.get("tags", [])),
        "allowed_uses": list(row.get("allowed_uses", [])),
        "review_status": row.get("review_status"),
        "created_at": row.get("created_at"),
        "reviewed_at": row.get("reviewed_at"),
        "is_latest": True,
        "reusable": True,
        "stale_fact_ids": [],
    }


def create_career_story_selection_router(store) -> APIRouter:
    """List current approved revisions that a user may select for this job/use."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/jobs/{job_id}/career-stories/selectable")
    def selectable_career_stories(
        profile_id: str,
        job_id: str,
        allowed_use: str | None = Query(default=None, min_length=1, max_length=32),
    ):
        if allowed_use not in _ALLOWED_USE_FILTERS:
            raise HTTPException(
                422, {"code": "CAREER_STORY_SELECTION_USE_INVALID"},
            )
        profile = _profile(store, profile_id)
        job = _job(store, job_id)
        try:
            selection = select_stories_for_job(
                store, profile_id, job_id, allowed_use,
            )
        except StorySelectionError as error:
            raise HTTPException(
                error.status_code,
                {"code": f"CAREER_STORY_SELECTION_{error.code}"},
            ) from None

        selectable = [
            _selection_row(row)
            for row in selection["stories"]
        ]

        # A profile/job could change between reads. Do not return choices bound to
        # mismatched snapshots that a later consumer might mistake for current.
        current_profile = _profile(store, profile_id)
        current_job = _job(store, job_id)
        if (current_profile["version"] != profile["version"]
                or current_job["fingerprint"] != job["fingerprint"]):
            raise HTTPException(
                409, {"code": "CAREER_STORY_SELECTION_CONTEXT_STALE"},
            )

        return {
            "schema_version": 1,
            "profile_id": profile_id,
            "job_id": job_id,
            "profile_version": profile["version"],
            "job_fingerprint": job["fingerprint"],
            "allowed_use": allowed_use,
            "stories": selectable,
        }

    return router
