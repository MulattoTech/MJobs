"""Reviewed, profile/job-scoped resume workspace editing endpoints."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.documents.resume_templates import list_resume_templates
from services.documents.resume_workspace import list_resume_workspace_revisions, ResumeWorkspaceError
from services.documents.resume_workspace_export import (
    read_reviewed_resume_workspace_export,
    save_reviewed_resume_workspace_export,
)
from services.documents.resume_workspace_preview import build_resume_workspace_draft_preview


class Section(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    section_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    visible: bool
    position: int = Field(ge=0, le=49)
    fact_ids: list[str] = Field(min_length=1, max_length=100)


class PreviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    template_id: str = Field(min_length=1, max_length=96)
    sections: list[Section] = Field(min_length=1, max_length=50)
    expected_profile_version: str = Field(min_length=1, max_length=128)
    expected_job_fingerprint: str = Field(min_length=1, max_length=128)
    expected_template_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def layout(self):
        ids = [s.section_id for s in self.sections]
        positions = [s.position for s in self.sections]
        if len(ids) != len(set(ids)) or sorted(positions) != list(range(len(positions))) or not any(s.visible for s in self.sections):
            raise ValueError("invalid resume workspace layout")
        return self


class SaveInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    expected_version: int | None
    profile_version: str = Field(min_length=1, max_length=128)
    job_fingerprint: str = Field(min_length=1, max_length=128)
    template_id: str = Field(min_length=1, max_length=96)
    sections: list[Section] = Field(min_length=1, max_length=50)
    preview_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    reviewed: bool
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")

    @model_validator(mode="after")
    def layout(self):
        ids = [s.section_id for s in self.sections]
        positions = [s.position for s in self.sections]
        if len(ids) != len(set(ids)) or sorted(positions) != list(range(len(positions))) or not any(s.visible for s in self.sections):
            raise ValueError("invalid resume workspace layout")
        return self


def _raise(error: Exception):
    code = getattr(error, "code", "RESUME_WORKSPACE_SERVICE_ERROR")
    status = 409 if ("STALE" in code or "CONFLICT" in code) else 404 if "NOT_FOUND" in code else 422 if code in {
        "RESUME_WORKSPACE_INVALID", "RESUME_WORKSPACE_REVIEW_REQUIRED", "RESUME_WORKSPACE_PREVIEW_STALE",
    } else 500
    raise HTTPException(status, {"code": code}) from None


def _json_row(row):
    value = dict(row)
    if isinstance(value.get("created_at"), datetime):
        value["created_at"] = value["created_at"].isoformat()
    return value


def create_resume_workspace_editor_router(store):
    router = APIRouter()

    @router.get("/resume-workspace/templates")
    def templates():
        return {"templates": list_resume_templates()}

    @router.get("/profiles/{profile_id}/jobs/{job_id}/resume-workspace")
    def read(profile_id: str, job_id: str):
        try:
            versions = [_json_row(row) for row in list_resume_workspace_revisions(store, profile_id, job_id)]
        except Exception as error:
            if getattr(error, "code", None) == "RESUME_WORKSPACE_INVALID":
                raise HTTPException(404, {"code": "RESUME_WORKSPACE_NOT_FOUND"}) from None
            _raise(error)
        return {"profile_id": profile_id, "job_id": job_id,
                "current": versions[-1] if versions else None, "versions": versions}

    @router.post("/profiles/{profile_id}/jobs/{job_id}/resume-workspace/preview")
    def preview(profile_id: str, job_id: str, value: PreviewInput):
        try:
            result = build_resume_workspace_draft_preview(
                store, profile_id, job_id,
                template_id=value.template_id,
                sections=[item.model_dump() for item in value.sections],
                expected_profile_version=value.expected_profile_version,
                expected_job_fingerprint=value.expected_job_fingerprint,
                expected_template_version=value.expected_template_version,
            )
        except Exception as error:
            _raise(error)
        return result

    @router.put("/profiles/{profile_id}/jobs/{job_id}/resume-workspace")
    def save(profile_id: str, job_id: str, value: SaveInput):
        try:
            row = save_reviewed_resume_workspace_export(
                store, profile_id=profile_id, job_id=job_id,
                expected_version=value.expected_version,
                profile_version=value.profile_version,
                job_fingerprint=value.job_fingerprint,
                template_id=value.template_id,
                sections=[item.model_dump() for item in value.sections],
                preview_fingerprint=value.preview_fingerprint,
                reviewed=value.reviewed, request_id=value.request_id,
            )
        except Exception as error:
            _raise(error)
        replayed = row.pop("replayed", False)
        return {"workspace_revision": _json_row(row), "replayed": replayed}

    @router.get("/profiles/{profile_id}/jobs/{job_id}/resume-workspace/versions/{version}/text")
    def export(profile_id: str, job_id: str, version: int):
        try:
            text = read_reviewed_resume_workspace_export(store, profile_id, job_id, version)
        except Exception as error:
            if getattr(error, "code", None) == "RESUME_WORKSPACE_INVALID":
                raise HTTPException(404, {"code": "RESUME_WORKSPACE_NOT_FOUND"}) from None
            _raise(error)
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="resume-workspace-v{version}.txt"'})

    return router
