"""Profile-scoped API route for sanitized company discovery activity."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import NoResultFound

from services.company_discovery.activity import list_company_discovery_activity


def create_company_discovery_activity_router(store) -> APIRouter:
    """Build the read-only company discovery activity endpoint."""
    router = APIRouter()

    @router.get("/company-discovery/activity")
    def company_discovery_activity(
        profile_id: str = Query(..., min_length=1, max_length=128),
    ):
        try:
            events = list_company_discovery_activity(store, profile_id)
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "COMPANY_DISCOVERY_PROFILE_NOT_FOUND"},
            ) from None
        return {"events": events}

    return router
