"""Explicit job-view API; no application/disposition transitions."""
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import NoResultFound

from services.tracking.recently_viewed import (
    MAX_VIEWS_PER_PROFILE, clear_recent_job_views, list_recent_job_views, record_job_view,
)


def create_recently_viewed_router(store) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles/{profile_id}/jobs/recently-viewed")
    def recent_jobs(profile_id: str, limit: int = Query(default=100, ge=1, le=MAX_VIEWS_PER_PROFILE)):
        try:
            return list_recent_job_views(store, profile_id, limit=limit)
        except NoResultFound:
            raise HTTPException(404, detail={"code": "RECENT_JOBS_PROFILE_NOT_FOUND"}) from None
        except ValueError:
            raise HTTPException(422, detail={"code": "RECENT_JOBS_REQUEST_INVALID"}) from None

    @router.post("/profiles/{profile_id}/jobs/{job_id}/view", status_code=201)
    def mark_viewed(profile_id: str, job_id: str):
        try:
            return record_job_view(store, profile_id, job_id)
        except NoResultFound:
            raise HTTPException(404, detail={"code": "RECENT_JOBS_PROFILE_OR_JOB_NOT_FOUND"}) from None
        except ValueError:
            raise HTTPException(422, detail={"code": "RECENT_JOBS_REQUEST_INVALID"}) from None

    @router.delete("/profiles/{profile_id}/jobs/recently-viewed")
    def forget_views(profile_id: str):
        try:
            return clear_recent_job_views(store, profile_id)
        except NoResultFound:
            raise HTTPException(404, detail={"code": "RECENT_JOBS_PROFILE_NOT_FOUND"}) from None
        except ValueError:
            raise HTTPException(422, detail={"code": "RECENT_JOBS_REQUEST_INVALID"}) from None

    return router
