"""Profile-scoped API for durable, non-submitting application page progress."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import NoResultFound

from services.applications.multipage_state import (
    load_multipage_state,
    transition_multipage_state,
)


class MultipageStateRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    action: Literal["SAVE_PROGRESS", "NEXT", "PREVIOUS", "REVIEW"]
    expected_version: int = Field(ge=0)
    page_id: str = Field(min_length=1, max_length=128)
    page_order: list[str] | None = None
    next_page_id: str | None = Field(default=None, min_length=1, max_length=128)
    previous_page_id: str | None = Field(default=None, min_length=1, max_length=128)
    progress: dict[str, object]


_ERROR_STATUS = {
    "APPLICATION_NOT_FOUND": 404,
    "APPLICATION_NOT_EDITABLE": 409,
    "MULTIPAGE_VERSION_CONFLICT": 409,
    "MULTIPAGE_REQUEST_ID_CONFLICT": 409,
    "INVALID_REQUEST": 422,
    "INVALID_REQUEST_ID": 422,
    "INVALID_ACTION": 422,
    "INVALID_VERSION": 422,
    "INVALID_PAGE_ID": 422,
    "INVALID_PAGE_ORDER": 422,
    "INVALID_PROGRESS": 422,
    "INVALID_TRANSITION": 422,
}


def _raise_service_error(error: Exception) -> None:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code:
        code = "MULTIPAGE_STATE_INVALID"
    raise HTTPException(_ERROR_STATUS.get(code, 422), {"code": code}) from None


def create_application_multipage_state_router(store) -> APIRouter:
    """Expose durable page state without adding a submission operation."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/applications/{application_id}/multipage-state")
    def get_multipage_state(profile_id: str, application_id: str):
        try:
            return load_multipage_state(store, profile_id, application_id)
        except NoResultFound:
            raise HTTPException(
                404, {"code": "MULTIPAGE_STATE_NOT_FOUND"},
            ) from None
        except (KeyError, TypeError, ValueError) as error:
            _raise_service_error(error)

    @router.post("/profiles/{profile_id}/applications/{application_id}/multipage-state")
    def update_multipage_state(profile_id: str, application_id: str,
                               request: MultipageStateRequest):
        try:
            return transition_multipage_state(
                store,
                profile_id,
                application_id,
                request.model_dump(exclude_unset=True),
            )
        except NoResultFound:
            raise HTTPException(
                404, {"code": "MULTIPAGE_STATE_APPLICATION_NOT_FOUND"},
            ) from None
        except (KeyError, TypeError, ValueError) as error:
            _raise_service_error(error)

    return router
