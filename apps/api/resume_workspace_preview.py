"""Profile/job-scoped read-only resume workspace preview API."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from services.documents.errors import PreparationError


class ResumeWorkspacePreviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    workspace_version: int = Field(ge=1)
    expected_profile_version: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    expected_job_fingerprint: str = Field(min_length=64, max_length=64,
                                          pattern=r"^[0-9a-f]{64}$")
    expected_template_version: str = Field(min_length=1, max_length=64, pattern=r"^\S+$")


_STALE_CODES = {
    "RESUME_WORKSPACE_PROFILE_STALE",
    "RESUME_WORKSPACE_JOB_STALE",
    "RESUME_WORKSPACE_STALE_CONTEXT",
    "RESUME_TEMPLATE_VERSION_STALE",
}


def _default_preview_builder(store, profile_id, job_id, **snapshot):
    from services.documents.resume_workspace_preview import build_resume_workspace_preview

    return build_resume_workspace_preview(store, profile_id, job_id, **snapshot)


def _validate_preview(result, profile_id, job_id, request):
    if not isinstance(result, dict):
        raise HTTPException(500, {"code": "RESUME_WORKSPACE_PREVIEW_READBACK_INVALID"})
    template = result.get("template")
    if (result.get("profile_id") != profile_id or result.get("job_id") != job_id or
            result.get("profile_version") != request.expected_profile_version or
            result.get("job_fingerprint") != request.expected_job_fingerprint or
            result.get("workspace_version") != request.workspace_version or
            result.get("read_only") is not True or result.get("text_only") is not True or
            result.get("ats_safe") is not True or result.get("document_generated") is not False or
            not isinstance(result.get("preview_text"), str) or
            not isinstance(result.get("sections"), list) or
            not isinstance(result.get("rendered_sections"), list) or
            not isinstance(result.get("source_refs"), dict) or
            not isinstance(result.get("keyword_alignment"), dict) or
            not isinstance(template, dict) or
            template.get("version") != request.expected_template_version or
            template.get("preview_only") is not True or
            template.get("ats_safe") is not True):
        raise HTTPException(500, {"code": "RESUME_WORKSPACE_PREVIEW_READBACK_INVALID"})
    return result


def create_resume_workspace_preview_router(store, *, preview_builder: Callable | None = None):
    """Create a read-only preview route over an exact saved workspace revision."""
    preview_builder = preview_builder or _default_preview_builder
    router = APIRouter()

    @router.post("/profiles/{profile_id}/jobs/{job_id}/resume-workspace/preview")
    def preview_resume_workspace(profile_id: str, job_id: str,
                                 value: ResumeWorkspacePreviewInput):
        if (not profile_id.strip() or not job_id.strip() or len(profile_id) > 128 or
                len(job_id) > 128):
            raise HTTPException(404, {"code": "RESUME_WORKSPACE_NOT_FOUND"})
        snapshot = {
            "workspace_version": value.workspace_version,
            "expected_profile_version": value.expected_profile_version,
            "expected_job_fingerprint": value.expected_job_fingerprint,
            "expected_template_version": value.expected_template_version,
        }
        try:
            result = preview_builder(store, profile_id, job_id, **snapshot)
        except PreparationError as error:
            status = 409 if error.code in _STALE_CODES else 422
            raise HTTPException(status, {"code": error.code}) from None
        except LookupError:
            raise HTTPException(404, {"code": "RESUME_WORKSPACE_NOT_FOUND"}) from None
        except ValueError as error:
            code = getattr(error, "code", "RESUME_WORKSPACE_PREVIEW_INVALID")
            status = 409 if code in _STALE_CODES else 422
            raise HTTPException(status, {"code": code}) from None
        except Exception:
            raise HTTPException(503, {"code": "RESUME_WORKSPACE_PREVIEW_UNAVAILABLE"}) from None
        return _validate_preview(result, profile_id, job_id, value)

    return router
