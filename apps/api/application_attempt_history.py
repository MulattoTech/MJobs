"""Profile-scoped, read-only durable application execution history."""
from fastapi import APIRouter, HTTPException
from sqlalchemy.orm.exc import NoResultFound

from services.applications.attempt_history import read_attempt_history


def create_application_attempt_history_router(store):
    router = APIRouter()

    @router.get("/profiles/{profile_id}/applications/{application_id}/attempt-history")
    def get_history(profile_id: str, application_id: str):
        try:
            return read_attempt_history(store, profile_id, application_id)
        except NoResultFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None

    return router
