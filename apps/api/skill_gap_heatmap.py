"""Profile-scoped read-only skill-gap heatmap endpoint."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import NoResultFound

from services.learning.skill_gap_heatmap import get_skill_gap_heatmap


def create_skill_gap_heatmap_router(store) -> APIRouter:
    """Build the deterministic heatmap endpoint for one profile."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/skill-gap-heatmap")
    def skill_gap_heatmap(
        profile_id: str,
        profile_version: str | None = Query(default=None, min_length=64, max_length=64),
    ):
        try:
            heatmap = get_skill_gap_heatmap(
                store, profile_id, profile_version=profile_version,
            )
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "SKILL_GAP_PROFILE_NOT_FOUND"},
            ) from None
        except ValueError as error:
            if "stale" in str(error).casefold():
                raise HTTPException(
                    status_code=409,
                    detail={"code": "SKILL_GAP_PROFILE_VERSION_STALE"},
                ) from None
            raise

        return {"profile_id": profile_id, **heatmap}

    return router
