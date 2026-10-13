"""Owner-scoped consent for synthetic conditional-field dry-run only."""
from fastapi import APIRouter, Body, HTTPException

from apps.api.application_rehearsal import _validate_review_payload, _validate_scope
from services.applications import mock_dynamic_review as service
from services.applications.rehearsal_store import RehearsalStoreError


def create_mock_dynamic_review_router(store):
    router = APIRouter()
    path = "/profiles/{profile_id}/applications/{application_id}/mock-dynamic-review"

    def invoke(function, *args):
        try:
            return function(*args)
        except RehearsalStoreError as error:
            raise HTTPException(404 if error.code == "APPLICATION_NOT_FOUND" else 409,
                                {"code": error.code}) from None
        except Exception:
            raise HTTPException(503, {"code": "MOCK_DYNAMIC_REVIEW_UNAVAILABLE"}) from None

    @router.get(path)
    def read(profile_id: str, application_id: str):
        _validate_scope(profile_id, application_id)
        return invoke(service.read, store, profile_id, application_id)

    @router.post(path)
    def review(profile_id: str, application_id: str, payload: object = Body(...)):
        _validate_scope(profile_id, application_id)
        request_id, fingerprint = _validate_review_payload(payload)
        return invoke(service.acknowledge, store, profile_id, application_id, request_id, fingerprint)

    return router
