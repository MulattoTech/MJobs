"""Profile-scoped local email preview and explicit status-review endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from services.tracking.email_status_store import (
    EmailStatusError,
    apply_email_status,
    list_email_status_history,
    preview_email_status,
)


_Status = Literal["APPLIED", "REJECTED", "INTERVIEW", "RECRUITER_CONTACT", "OFFER"]


class EmailStatusPreviewRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    content: str = Field(min_length=1, max_length=256 * 1024)
    format: Literal["text", "eml"] = "text"


class EmailStatusApplyRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=128,
                            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    status: _Status
    expected_tracking_sequence: int = Field(ge=0)
    reviewed: StrictBool


def _call(operation):
    try:
        return operation()
    except EmailStatusError as error:
        raise HTTPException(error.status_code, {"code": error.code}) from None


def create_email_status_router(store) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles/{profile_id}/applications/{application_id}/email-status")
    def read_email_status(profile_id: str, application_id: str):
        return _call(lambda: list_email_status_history(store, profile_id, application_id))

    @router.post("/profiles/{profile_id}/applications/{application_id}/email-status/preview")
    def preview_email(profile_id: str, application_id: str,
                      value: EmailStatusPreviewRequest):
        return _call(lambda: preview_email_status(
            store, profile_id, application_id, value.content, format=value.format))

    @router.post(
        "/profiles/{profile_id}/applications/{application_id}/email-status/{preview_id}/apply",
    )
    def apply_email(profile_id: str, application_id: str, preview_id: str,
                    value: EmailStatusApplyRequest):
        return _call(lambda: apply_email_status(
            store, profile_id, application_id, preview_id, value.request_id,
            value.status, value.expected_tracking_sequence, value.reviewed))

    return router


__all__ = ["create_email_status_router"]
