"""Profile-scoped preview and local review receipt routes."""
from __future__ import annotations

import re

from fastapi import APIRouter, Body, HTTPException

from services.applications.rehearsal import RehearsalError
from services.applications.rehearsal_store import (
    RehearsalStoreError,
    acknowledge_application_rehearsal,
    get_application_rehearsal,
)


_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def _error_response(error: Exception) -> HTTPException:
    code = getattr(error, "code", None)
    if code in {"APPLICATION_NOT_FOUND", "PROFILE_NOT_FOUND"}:
        return HTTPException(404, {"code": "APPLICATION_NOT_FOUND"})
    if code == "FORM_REVIEW_REQUIRED":
        return HTTPException(409, {"code": "APPLICATION_REHEARSAL_FORM_REVIEW_REQUIRED"})
    if code == "FORM_REVIEW_STALE":
        return HTTPException(409, {"code": "APPLICATION_REHEARSAL_STALE"})
    if code == "FORM_ANALYSIS_INVALID":
        return HTTPException(409, {"code": "APPLICATION_REHEARSAL_FORM_ANALYSIS_INVALID"})
    if code in {
        "APPLICATION_REHEARSAL_STALE",
        "APPLICATION_REHEARSAL_REQUEST_ID_REUSED",
    }:
        return HTTPException(409, {"code": code})
    if code in {
        "APPLICATION_REHEARSAL_INVALID_REQUEST",
        "APPLICATION_REHEARSAL_INVALID",
    }:
        return HTTPException(422, {"code": code})
    # Do not echo unexpected service or persistence exceptions.
    return HTTPException(503, {"code": "APPLICATION_REHEARSAL_UNAVAILABLE"})


def _validate_review_payload(payload: object) -> tuple[str, str]:
    if (not isinstance(payload, dict)
            or set(payload) != {"request_id", "fingerprint", "acknowledged"}):
        raise HTTPException(422, {"code": "APPLICATION_REHEARSAL_INVALID_REQUEST"})
    request_id = payload["request_id"]
    fingerprint = payload["fingerprint"]
    if (not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id)
            or not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint)
            or payload["acknowledged"] is not True):
        raise HTTPException(422, {"code": "APPLICATION_REHEARSAL_INVALID_REQUEST"})
    return request_id, fingerprint


def _validate_scope(profile_id: str, application_id: str) -> None:
    if (not isinstance(profile_id, str) or not _REQUEST_ID.fullmatch(profile_id)
            or not isinstance(application_id, str)
            or not _REQUEST_ID.fullmatch(application_id)):
        raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"})


def create_application_rehearsal_router(store) -> APIRouter:
    """Create the read-only rehearsal endpoint and durable acknowledgement route."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/applications/{application_id}/rehearsal")
    def get_rehearsal(profile_id: str, application_id: str):
        _validate_scope(profile_id, application_id)
        try:
            return get_application_rehearsal(store, profile_id, application_id)
        except (RehearsalError, RehearsalStoreError) as error:
            raise _error_response(error) from None
        except Exception:
            raise HTTPException(
                503, {"code": "APPLICATION_REHEARSAL_UNAVAILABLE"},
            ) from None

    @router.post("/profiles/{profile_id}/applications/{application_id}/rehearsal/reviews")
    def acknowledge_rehearsal(profile_id: str, application_id: str,
                              payload: object = Body(...)):
        _validate_scope(profile_id, application_id)
        request_id, fingerprint = _validate_review_payload(payload)
        try:
            return acknowledge_application_rehearsal(
                store, profile_id, application_id, request_id, fingerprint,
                acknowledged=True,
            )
        except (RehearsalError, RehearsalStoreError) as error:
            raise _error_response(error) from None
        except Exception:
            raise HTTPException(
                503, {"code": "APPLICATION_REHEARSAL_UNAVAILABLE"},
            ) from None

    return router
