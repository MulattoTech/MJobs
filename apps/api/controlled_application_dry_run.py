"""Local fixture-only ATS preparation routes. External submission stays OFF."""
from fastapi import APIRouter, Body, HTTPException
from pydantic import Field, field_validator
from typing import Literal

from apps.api.application_form_review import FormReviewRequest
from apps.api.application_rehearsal import _validate_review_payload, _validate_scope
from services.applications import controlled_dry_run as service
from services.applications.rehearsal import RehearsalError
from services.applications.rehearsal_store import (
    RehearsalStoreError, acknowledge_application_rehearsal,
)
from services.applications.supporting_documents import SupportingDocumentError, select_documents, list_selection


class RunRequest(FormReviewRequest):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class RetryRequest(RunRequest):
    acknowledged: Literal[True]

    @field_validator('acknowledged', mode='before')
    @classmethod
    def explicit_acknowledgement(cls, value):
        if value is not True:
            raise ValueError('CONTROLLED_DRY_RUN_RETRY_REVIEW_REQUIRED')
        return value


def create_controlled_application_dry_run_router(store):
    router = APIRouter()
    prefix = "/profiles/{profile_id}/applications/{application_id}/controlled-dry-run"

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (service.ControlledDryRunError, RehearsalError, RehearsalStoreError,
                SupportingDocumentError) as error:
            code = error.code
            raise HTTPException(404 if code in {"APPLICATION_NOT_FOUND", "PROFILE_NOT_FOUND", "CONTROLLED_DRY_RUN_NOT_FOUND"}
                                else 409, {"code": code}) from None
        except Exception:
            raise HTTPException(503, {"code": "CONTROLLED_DRY_RUN_UNAVAILABLE"}) from None

    @router.get(prefix)
    def read_preview(profile_id: str, application_id: str):
        _validate_scope(profile_id, application_id)
        return invoke(service.reviewed_preview, store, profile_id, application_id)

    @router.post(prefix+"/reviews")
    def review(profile_id: str, application_id: str, payload: object = Body(...)):
        _validate_scope(profile_id, application_id)
        request_id, fingerprint = _validate_review_payload(payload)
        return invoke(acknowledge_application_rehearsal, store, profile_id, application_id,
                      request_id, fingerprint, True, preview_builder=service.preview)

    @router.post(prefix+"/runs", status_code=202)
    def queue(profile_id: str, application_id: str, value: RunRequest):
        _validate_scope(profile_id, application_id)
        return invoke(service.queue, store, profile_id, application_id,
                      value.request_id, value.fingerprint)

    @router.put(prefix+"/supporting-documents")
    def select_supporting(profile_id: str, application_id: str, payload: object = Body(...)):
        _validate_scope(profile_id, application_id)
        if not isinstance(payload, dict) or set(payload) != {"revision_ids", "request_id"}:
            raise HTTPException(422, {"code": "SUPPORTING_DOCUMENT_SELECTION_INVALID"})
        return invoke(select_documents, store, profile_id, application_id,
                      payload["revision_ids"], payload["request_id"], _controlled_fixture=True)

    @router.post(prefix+"/runs/{request_id}/cancel")
    def cancel(profile_id: str, application_id: str, request_id: str):
        _validate_scope(profile_id, application_id)
        return invoke(service.cancel, store, profile_id, application_id, request_id)

    @router.post(prefix+"/runs/{request_id}/retry", status_code=202)
    def retry(profile_id: str, application_id: str, request_id: str, value: RetryRequest):
        _validate_scope(profile_id, application_id)
        return invoke(service.queue, store, profile_id, application_id,
                      value.request_id, value.fingerprint, retry_of=request_id)

    @router.get(prefix+"/supporting-documents")
    def read_supporting(profile_id: str, application_id: str):
        _validate_scope(profile_id, application_id)
        invoke(service.require_scope, store, profile_id, application_id)
        return invoke(list_selection, store, profile_id, application_id, _controlled_fixture=True)

    @router.get(prefix+"/runs/latest")
    def latest(profile_id: str, application_id: str):
        _validate_scope(profile_id, application_id)
        invoke(service.require_scope, store, profile_id, application_id)
        return invoke(service.read_run, store, profile_id, application_id)

    @router.get(prefix+"/runs/{request_id}")
    def read(profile_id: str, application_id: str, request_id: str):
        _validate_scope(profile_id, application_id)
        invoke(service.require_scope, store, profile_id, application_id)
        result = invoke(service.read_run, store, profile_id, application_id, request_id)
        if result["request_id"] is None:
            raise HTTPException(404, {"code": "CONTROLLED_DRY_RUN_NOT_FOUND"})
        return result

    return router
