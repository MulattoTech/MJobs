"""Profile-scoped selection of supporting documents for local mock applications."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException


def create_application_supporting_documents_router(store) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles/{profile_id}/applications/{application_id}/supporting-documents")
    def get_supporting_documents(profile_id: str, application_id: str):
        from services.applications.supporting_documents import (
            SupportingDocumentNotFound, list_selection,
        )
        try:
            return list_selection(store, profile_id, application_id)
        except SupportingDocumentNotFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None
        except ValueError:
            raise HTTPException(503, {"code": "SUPPORTING_DOCUMENTS_UNAVAILABLE"}) from None

    @router.put("/profiles/{profile_id}/applications/{application_id}/supporting-documents")
    def put_supporting_documents(profile_id: str, application_id: str,
                                 payload: object = Body(...)):
        from services.applications.supporting_documents import (
            SupportingDocumentConflict, SupportingDocumentError,
            SupportingDocumentNotFound, select_documents,
        )
        if (not isinstance(payload, dict)
                or set(payload) != {"revision_ids", "request_id"}):
            raise HTTPException(422, {"code": "SUPPORTING_DOCUMENT_SELECTION_INVALID"})
        try:
            return select_documents(
                store, profile_id, application_id,
                payload["revision_ids"], payload["request_id"],
            )
        except SupportingDocumentNotFound:
            raise HTTPException(404, {"code": "APPLICATION_NOT_FOUND"}) from None
        except SupportingDocumentConflict as error:
            raise HTTPException(409, {"code": error.code}) from None
        except SupportingDocumentError as error:
            raise HTTPException(422, {"code": error.code}) from None
        except ValueError:
            raise HTTPException(503, {"code": "SUPPORTING_DOCUMENTS_UNAVAILABLE"}) from None

    return router
