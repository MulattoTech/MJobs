"""Profile-scoped API routes for archiving immutable document revisions."""
from __future__ import annotations

import re

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.documents.archive import archive_document, restore_document
from services.documents.archive_store import ARCHIVE_ACTIONS, list_archive_events


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_ARCHIVE = "DOCUMENT_ARCHIVED"


def _validated_operation_id(value: str) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise HTTPException(400, {"code": "DOCUMENT_ARCHIVE_IDEMPOTENCY_KEY_INVALID"})
    return value


def document_archive_metadata(store, profile_id: str, application_id: str | None,
                              document_id: str) -> dict:
    """Return the latest durable archive flag for one owned revision."""
    events = list_archive_events(
        store, profile_id=profile_id, application_id=application_id,
        document_id=document_id, limit=1000,
    )
    latest = events[-1] if events else None
    archived = bool(latest and latest["action"] == _ARCHIVE)
    return {
        "archived": archived,
        "archived_at": latest["timestamp"] if archived else None,
    }


def _change_document(store, action, profile_id: str, document_id: str,
                     operation_id: str) -> dict:
    try:
        _check_idempotency_scope(store, profile_id, document_id, action, operation_id)
        result = action(store, profile_id=profile_id, document_id=document_id,
                        operation_id=operation_id)
        metadata = document_archive_metadata(
            store, profile_id, result["application_id"], document_id,
        )
    except LookupError:
        raise HTTPException(404, {"code": "DOCUMENT_NOT_FOUND"}) from None
    except NoResultFound:
        raise HTTPException(404, {"code": "DOCUMENT_NOT_FOUND"}) from None
    except ValueError as error:
        if "reused" in str(error).casefold():
            raise HTTPException(409, {"code": "DOCUMENT_ARCHIVE_IDEMPOTENCY_CONFLICT"}) from None
        raise HTTPException(422, {"code": "DOCUMENT_ARCHIVE_REQUEST_INVALID"}) from None

    return {
        "document_id": result["document_id"],
        "archived": metadata["archived"],
        "archived_at": metadata["archived_at"],
        "changed": bool(result["changed"]),
    }


def _check_idempotency_scope(store, profile_id: str, document_id: str,
                             action, operation_id: str) -> None:
    requested_action = "DOCUMENT_ARCHIVED" if action is archive_document else "DOCUMENT_ARCHIVE_RESTORED"
    with store.engine.connect() as connection:
        prior = connection.execute(select(
            m.audit.c.action,
            m.audit.c.payload["document_id"].as_string().label("document_id"),
        ).where(
            m.audit.c.action.in_(ARCHIVE_ACTIONS),
            m.audit.c.payload["profile_id"].as_string() == profile_id,
            m.audit.c.payload["operation_id"].as_string() == operation_id,
        )).mappings()
        if any(row["action"] != requested_action or row["document_id"] != document_id
               for row in prior):
            raise HTTPException(409, {"code": "DOCUMENT_ARCHIVE_IDEMPOTENCY_CONFLICT"})


def create_document_archive_router(store) -> APIRouter:
    """Build explicit archive/restore endpoints for immutable document revisions."""
    router = APIRouter()

    @router.post("/profiles/{profile_id}/document-library/{document_id}/archive")
    def archive_profile_document(
        profile_id: str,
        document_id: str,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        operation_id = _validated_operation_id(idempotency_key)
        return _change_document(store, archive_document, profile_id, document_id, operation_id)

    @router.post("/profiles/{profile_id}/document-library/{document_id}/restore")
    def restore_profile_document(
        profile_id: str,
        document_id: str,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        operation_id = _validated_operation_id(idempotency_key)
        return _change_document(store, restore_document, profile_id, document_id, operation_id)

    return router
