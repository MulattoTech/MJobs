"""Profile-scoped API route for sanitized company discovery activity."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import NoResultFound

from services.company_discovery.activity import list_company_discovery_activity


def create_company_discovery_activity_router(store) -> APIRouter:
    """Build the read-only company discovery activity endpoint."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/company-discovery/activity")
    def company_discovery_activity(
        profile_id: str,
        limit: int = Query(100, ge=1, le=200),
    ):
        try:
            events = list_company_discovery_activity(store, profile_id, limit=limit)
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "COMPANY_DISCOVERY_PROFILE_NOT_FOUND"},
            ) from None
        return {"profile_id": profile_id, "events": events}

    return router
