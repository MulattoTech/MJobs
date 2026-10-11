"""Profile-scoped API for durable company discovery queue status."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import NoResultFound

from services.company_discovery.queue_status import read_company_discovery_queue_status


def create_company_discovery_queue_router(store) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles/{profile_id}/company-discovery/queue")
    def company_discovery_queue(
        profile_id: str,
        limit: int = Query(100, ge=1, le=200),
    ):
        try:
            status = read_company_discovery_queue_status(store, profile_id, limit=limit)
        except (NoResultFound, LookupError):
            raise HTTPException(
                status_code=404,
                detail={"code": "COMPANY_DISCOVERY_PROFILE_NOT_FOUND"},
            ) from None
        except ValueError as error:
            code = str(error)
            if code == "COMPANY_DISCOVERY_QUEUE_LIMIT_INVALID":
                raise HTTPException(status_code=422, detail={"code": code}) from None
            raise
        return {
            "profile_id": profile_id,
            "counts": status["counts"],
            "activity_counts": status["activity_counts"],
            "items": [
                {
                    "request_id": row["id"],
                    "company": row["company"],
                    "state": row["state"],
                    "priority": row["priority"],
                    "updated_at": row["updated_at"],
                    "attempt_count": row["attempt_count"],
                }
                for row in status["items"]
            ],
        }

    return router
