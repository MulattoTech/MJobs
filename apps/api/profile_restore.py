"""Explicit, fresh-profile restore preview and import endpoints."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.persistence.profile_restore import (
    ProfileRestoreConflict,
    ProfileRestoreError,
    ProfileRestoreFileError,
    ProfileRestoreInvalidPlan,
    apply_profile_restore,
)


_MAX_REQUEST_BYTES = 5 * 1024 * 1024 + 8192
_FINGERPRINT = re.compile(r"^[0-9a-fA-F]{64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup: dict


class _RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup: dict
    request_id: str = Field(min_length=1, max_length=128, pattern=_REQUEST_ID.pattern)
    expected_fingerprint: str = Field(pattern=_FINGERPRINT.pattern)
    confirmed: Literal[True]


async def _body(request: Request, model):
    try:
        content = await request.body()
    except Exception:
        raise HTTPException(400, detail={"code": "PROFILE_RESTORE_REQUEST_INVALID"}) from None
    if len(content) > _MAX_REQUEST_BYTES:
        raise HTTPException(413, detail={"code": "PROFILE_RESTORE_TOO_LARGE"})
    try:
        decoded = json.loads(content)
        return model.model_validate(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        raise HTTPException(422, detail={"code": "PROFILE_RESTORE_REQUEST_INVALID"}) from None


def _service_functions():
    from services.profile_restore import build_restore_plan, preview_profile_restore
    return preview_profile_restore, build_restore_plan


def create_profile_restore_router(store, output_dir) -> APIRouter:
    """Create bounded preview/import routes for restoring into a fresh profile."""
    router = APIRouter()
    storage_root = Path(output_dir)

    @router.post("/profile-restores/preview")
    async def preview(request: Request):
        value = await _body(request, _PreviewRequest)
        try:
            preview_profile_restore, _ = _service_functions()
            return preview_profile_restore(value.backup)
        except Exception:
            # Parser errors may otherwise contain imported values or document data.
            raise HTTPException(422, detail={"code": "PROFILE_RESTORE_INVALID"}) from None

    @router.post("/profile-restores")
    async def restore(request: Request):
        value = await _body(request, _RestoreRequest)
        try:
            preview_profile_restore, build_restore_plan = _service_functions()
            preview_value = preview_profile_restore(value.backup)
        except Exception:
            raise HTTPException(422, detail={"code": "PROFILE_RESTORE_INVALID"}) from None
        fingerprint = preview_value.get("fingerprint") if isinstance(preview_value, dict) else None
        if (not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint)
                or fingerprint.casefold() != value.expected_fingerprint.casefold()):
            raise HTTPException(409, detail={"code": "PROFILE_RESTORE_PREVIEW_STALE"})

        profile_id = uuid4().hex
        try:
            plan = build_restore_plan(value.backup, profile_id)
            if (not isinstance(plan, dict)
                    or plan.get("fingerprint", "").casefold() != fingerprint.casefold()
                    or not isinstance(plan.get("profile"), dict)
                    or plan["profile"].get("id") != profile_id):
                raise ValueError("invalid restore plan")
            result = apply_profile_restore(store, plan, storage_root, value.request_id)
        except ProfileRestoreConflict:
            raise HTTPException(409, detail={"code": "PROFILE_RESTORE_CONFLICT"}) from None
        except ProfileRestoreInvalidPlan:
            raise HTTPException(422, detail={"code": "PROFILE_RESTORE_INVALID"}) from None
        except ProfileRestoreFileError:
            raise HTTPException(500, detail={"code": "PROFILE_RESTORE_FILE_WRITE_FAILED"}) from None
        except ProfileRestoreError:
            raise HTTPException(500, detail={"code": "PROFILE_RESTORE_FAILED"}) from None
        except Exception:
            raise HTTPException(422, detail={"code": "PROFILE_RESTORE_INVALID"}) from None

        response = {"profile_id": result["profile_id"], "request_id": value.request_id}
        return JSONResponse(status_code=200 if result.get("replayed") else 201,
                            content=response)

    return router
