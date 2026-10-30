"""Durable, profile-scoped local extraction for immutable uploaded resumes."""
from __future__ import annotations

import re

from sqlalchemy import insert, select

from packages.domain.operation_types import validate_operation_type
from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.career_profile.extraction import extract_resume
from services.documents.uploads import UploadIntegrityError, read_resume


OPERATION_KIND = "RESUME_EXTRACT_QUEUED"
EXTRACTOR_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DurableExtractionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _params(profile_id: str, resume_id: str, resume_sha256: str, request_id: str,
            extractor_version: int = EXTRACTOR_VERSION) -> dict:
    if (not isinstance(profile_id, str) or not profile_id
            or not isinstance(resume_id, str) or not resume_id
            or not isinstance(resume_sha256, str) or not _SHA256.fullmatch(resume_sha256)
            or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id)
            or type(extractor_version) is not int or extractor_version != EXTRACTOR_VERSION):
        raise DurableExtractionError("RESUME_EXTRACTION_IDENTITY_INVALID")
    return {
        "profile_id": profile_id,
        "resume_id": resume_id,
        "resume_sha256": resume_sha256,
        "request_id": request_id,
        "extractor_version": extractor_version,
    }


def queue_extraction(store, profile_id: str, record: dict, request_id: str) -> dict:
    """Insert-or-read one explicit user intent; never reopen a terminal operation."""
    validate_operation_type(OPERATION_KIND)
    params = _params(profile_id, record.get("id"), record.get("sha256"), request_id)
    operation_id = identity(OPERATION_KIND, profile_id, {"profile_id": profile_id, "request_id": request_id})
    values = {
        "id": operation_id,
        "kind": OPERATION_KIND,
        "entity_id": params["resume_id"],
        "params": params,
        "state": "QUEUED",
        "error": None,
        "updated_at": now(),
    }
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        elif connection.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            raise DurableExtractionError("RESUME_EXTRACTION_DATABASE_UNSUPPORTED")
        inserted_id = connection.execute(dialect_insert(m.operations).values(**values).on_conflict_do_nothing(
            index_elements=[m.operations.c.id]).returning(m.operations.c.id)).scalar_one_or_none()
        current = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id).with_for_update()).mappings().one()
        if (current["kind"] != OPERATION_KIND or current["entity_id"] != params["resume_id"]
                or current["params"] != params):
            raise DurableExtractionError("RESUME_EXTRACTION_IDENTITY_CONFLICT")
        return {
            "operation_id": operation_id,
            "request_id": request_id,
            "state": current["state"],
            "current_step": current["current_step"],
            "progress": current["progress"],
            "replayed": inserted_id is None,
        }


class _CancellationCheck:
    """Expose the worker deadline/cancel fence through the parser's event interface."""
    def __init__(self, check):
        self.check = check

    def is_set(self):
        self.check()
        return False


def extract_for_operation(store, output_dir, operation: dict, check):
    params = operation.get("params")
    if (operation.get("kind") != OPERATION_KIND
            or not isinstance(params, dict)
            or operation.get("entity_id") != params.get("resume_id")
            or set(params) != {"profile_id", "resume_id", "resume_sha256", "request_id", "extractor_version"}):
        raise DurableExtractionError("RESUME_EXTRACTION_OPERATION_INVALID")
    expected = _params(params["profile_id"], params["resume_id"],
                       params["resume_sha256"], params["request_id"], params["extractor_version"])
    try:
        record, data = read_resume(store, output_dir, expected["profile_id"], expected["resume_id"])
    except Exception as error:
        if isinstance(error, UploadIntegrityError):
            raise DurableExtractionError("RESUME_EXTRACTION_SOURCE_INVALID") from None
        raise
    if record["sha256"] != expected["resume_sha256"]:
        raise DurableExtractionError("RESUME_EXTRACTION_SOURCE_CHANGED")
    check()
    result = extract_resume(
        data, record["format"],
        f"uploaded-resume:{record['id']}:sha256:{record['sha256']}",
        cancellation_event=_CancellationCheck(check),
    )
    check()
    return result


def persist_result(connection, operation: dict, result: dict):
    """Write extracted fields inside the worker's fenced completion transaction."""
    params = _params(
        operation.get("params", {}).get("profile_id"),
        operation.get("params", {}).get("resume_id"),
        operation.get("params", {}).get("resume_sha256"),
        operation.get("params", {}).get("request_id"),
        operation.get("params", {}).get("extractor_version"),
    )
    connection.execute(insert(m.resume_extraction_results).values(
        id=operation["id"],
        profile_id=params["profile_id"],
        resume_id=params["resume_id"],
        resume_sha256=params["resume_sha256"],
        extractor_version=params["extractor_version"],
        result=result,
        created_at=now(),
    ))


def _safe_status(operation):
    return {
        "operation_id": operation["id"],
        "request_id": operation["params"].get("request_id"),
        "state": operation["state"],
        "current_step": operation["current_step"],
        "progress": operation["progress"],
        "error": operation["error"],
        "updated_at": operation["updated_at"],
        "completed_at": operation["completed_at"],
    }


def read_extraction(store, output_dir, profile_id: str, resume_id: str,
                    operation_id: str | None = None) -> dict:
    """Read only the owner's operation status and its completed suggestion result."""
    with store.engine.connect() as connection:
        record = connection.execute(select(m.uploaded_resumes).where(
            m.uploaded_resumes.c.id == resume_id,
            m.uploaded_resumes.c.profile_id == profile_id,
            m.uploaded_resumes.c.id.in_(select(m.resume_revisions.c.upload_id).where(
                m.resume_revisions.c.profile_id == profile_id,
                m.resume_revisions.c.document_kind == "BASE_RESUME")),
        )).mappings().one_or_none()
        if record is None:
            raise DurableExtractionError("RESUME_EXTRACTION_NOT_FOUND")
        record = dict(record)
        if operation_id is None:
            rows = list(connection.execute(select(m.operations).where(
                m.operations.c.kind == OPERATION_KIND,
                m.operations.c.entity_id == resume_id,
            ).order_by(m.operations.c.updated_at.desc(), m.operations.c.id.desc()).limit(100)).mappings())
            operation = next((dict(row) for row in rows
                              if isinstance(row["params"], dict)
                              and row["params"].get("profile_id") == profile_id), None)
        else:
            row = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
                m.operations.c.kind == OPERATION_KIND,
                m.operations.c.entity_id == resume_id,
            )).mappings().one_or_none()
            operation = dict(row) if row is not None else None
        if operation is None:
            raise DurableExtractionError("RESUME_EXTRACTION_NOT_FOUND")
        params = operation["params"]
        if (not isinstance(params, dict) or params.get("profile_id") != profile_id
                or params.get("resume_id") != resume_id
                or params.get("resume_sha256") != record["sha256"]
                or params.get("extractor_version") != EXTRACTOR_VERSION):
            raise DurableExtractionError("RESUME_EXTRACTION_NOT_FOUND")
        response = _safe_status(operation)
        if operation["state"] == "COMPLETED":
            try:
                checked_record, _ = read_resume(store, output_dir, profile_id, resume_id)
            except UploadIntegrityError:
                raise DurableExtractionError("RESUME_EXTRACTION_SOURCE_INVALID") from None
            except Exception:
                # Keep missing/foreign source indistinguishable from a missing scoped result.
                raise DurableExtractionError("RESUME_EXTRACTION_NOT_FOUND") from None
            if checked_record["sha256"] != params["resume_sha256"]:
                raise DurableExtractionError("RESUME_EXTRACTION_SOURCE_CHANGED")
            stored = connection.execute(select(m.resume_extraction_results).where(
                m.resume_extraction_results.c.id == operation["id"],
                m.resume_extraction_results.c.profile_id == profile_id,
                m.resume_extraction_results.c.resume_id == resume_id,
                m.resume_extraction_results.c.resume_sha256 == record["sha256"],
                m.resume_extraction_results.c.extractor_version == EXTRACTOR_VERSION,
            )).mappings().one_or_none()
            if stored is None:
                raise DurableExtractionError("RESUME_EXTRACTION_RESULT_UNAVAILABLE")
            response["result"] = stored["result"]
        return response
