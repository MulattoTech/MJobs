"""Profile/job-scoped API adapter for versioned ATS-safe resume layouts."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


_TEMPLATES = frozenset({
    "verified-headings-v1",
    "approved-skill-and-bullet-emphasis-v2",
    "approved-selection-and-bullet-emphasis-v2",
})
_SECTION_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ResumeWorkspaceSectionInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    section_id: str = Field(min_length=1, max_length=64)
    visible: bool
    position: int = Field(ge=0, le=49)
    fact_ids: list[Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]] = Field(
        min_length=1, max_length=100,
    )

    @model_validator(mode="after")
    def validate_identity(self):
        if not _SECTION_ID.fullmatch(self.section_id):
            raise ValueError("invalid section id")
        if len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("duplicate fact reference")
        return self


class ResumeWorkspaceSaveInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expected_version: int | None
    profile_version: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    job_fingerprint: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    template_id: str = Field(min_length=1, max_length=96)
    sections: list[ResumeWorkspaceSectionInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_layout(self):
        if self.expected_version is not None and self.expected_version < 1:
            raise ValueError("expected version must be positive or null")
        if self.template_id not in _TEMPLATES:
            raise ValueError("unsupported ATS-safe resume template")
        ids = [row.section_id for row in self.sections]
        positions = [row.position for row in self.sections]
        if len(set(ids)) != len(ids) or sorted(positions) != list(range(len(self.sections))):
            raise ValueError("layout sections and positions must be unique and contiguous")
        if not any(row.visible for row in self.sections):
            raise ValueError("at least one section must remain visible")
        return self


def _service_functions(list_revisions, save_revision):
    if list_revisions is not None and save_revision is not None:
        return list_revisions, save_revision
    try:
        from services.documents.resume_workspace import (
            list_resume_workspace_revisions,
            save_resume_workspace_revision,
        )
    except ImportError as error:
        raise RuntimeError(
            "Resume workspace service is not installed; provide its list/save callables."
        ) from error
    return list_revisions or list_resume_workspace_revisions, save_revision or save_resume_workspace_revision


def _service_http_error(error: Exception) -> HTTPException:
    code = getattr(error, "code", None)
    if code in {
        "RESUME_WORKSPACE_STALE_VERSION", "RESUME_WORKSPACE_STALE_CONTEXT",
        "RESUME_WORKSPACE_PROFILE_STALE", "RESUME_WORKSPACE_JOB_STALE",
    }:
        return HTTPException(409, {"code": code})
    if code in {"RESUME_WORKSPACE_NOT_FOUND", "RESUME_WORKSPACE_PROFILE_NOT_FOUND",
                "RESUME_WORKSPACE_JOB_NOT_FOUND"}:
        return HTTPException(404, {"code": code})
    if code in {"RESUME_WORKSPACE_INVALID", "RESUME_WORKSPACE_LAYOUT_INVALID"}:
        return HTTPException(422, {"code": code})
    if isinstance(error, (KeyError, LookupError)):
        return HTTPException(404, {"code": "RESUME_WORKSPACE_NOT_FOUND"})
    if isinstance(error, ValueError):
        return HTTPException(422, {"code": "RESUME_WORKSPACE_INVALID"})
    return HTTPException(500, {"code": "RESUME_WORKSPACE_SERVICE_ERROR"})


def _api_row(row: Any, *, profile_id: str, job_id: str) -> dict:
    if not isinstance(row, dict):
        raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
    revision = row.get("version")
    layout = row.get("sections")
    if (row.get("profile_id") != profile_id or row.get("job_id") != job_id
            or type(revision) is not int or revision < 1
            or not isinstance(row.get("profile_version"), str) or not row["profile_version"]
            or not isinstance(row.get("job_fingerprint"), str) or not row["job_fingerprint"]
            or not isinstance(row.get("template_id"), str)
            or row.get("template_id") not in _TEMPLATES
            or type(layout) is not list or not layout or len(layout) > 50):
        raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
    sections = []; section_ids = set(); has_visible = False
    for index, section in enumerate(layout):
        if (not isinstance(section, dict)
                or set(section) != {"section_id", "visible", "position", "fact_ids"}
                or not isinstance(section["section_id"], str)
                or not _SECTION_ID.fullmatch(section["section_id"])
                or section["section_id"] in section_ids
                or type(section["visible"]) is not bool
                or type(section["position"]) is not int or section["position"] != index
                or type(section["fact_ids"]) is not list
                or not section["fact_ids"]
                or any(not isinstance(fact_id, str) or not fact_id for fact_id in section["fact_ids"])
                or len(set(section["fact_ids"])) != len(section["fact_ids"])):
            raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
        section_ids.add(section["section_id"]);has_visible=has_visible or section["visible"]
        sections.append({
            "section_id": section["section_id"],
            "visible": section["visible"],
            "position": section["position"],
            "fact_ids": list(section["fact_ids"]),
        })
    if not has_visible:
        raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
    created_at = row.get("created_at")
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    if created_at is not None and not isinstance(created_at, str):
        raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
    return {
        "profile_id": profile_id,
        "job_id": job_id,
        "version": revision,
        "profile_version": row["profile_version"],
        "job_fingerprint": row["job_fingerprint"],
        "template_id": row["template_id"],
        "sections": sections,
        "created_at": created_at,
    }


def create_resume_workspace_router(store, *, list_revisions: Callable | None = None,
                                  save_revision: Callable | None = None) -> APIRouter:
    """Create routes bound to the durable resume-workspace service.

    Service interface: ``list_resume_workspace_revisions(store, profile_id, job_id)``
    returns ordered rows with ``version``, ``sections``, and exact profile/job scope;
    ``save_resume_workspace_revision(store, *, profile_id, job_id, expected_version,
    profile_version, job_fingerprint, template_id, sections)`` returns the persisted
    row. Both service calls own transactional profile/job/version checks and writes.
    """
    list_rows, save_row = _service_functions(list_revisions, save_revision)
    router = APIRouter()

    @router.get("/profiles/{profile_id}/jobs/{job_id}/resume-workspace")
    def get_resume_workspace(profile_id: str, job_id: str):
        try:
            rows = list_rows(store, profile_id, job_id)
        except Exception as error:
            raise _service_http_error(error) from None
        if type(rows) is not list:
            raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
        try:
            revisions = [_api_row(row, profile_id=profile_id, job_id=job_id) for row in rows]
        except HTTPException:
            raise
        versions = sorted(revisions, key=lambda row: row["version"])
        if len({row["version"] for row in versions}) != len(versions):
            raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
        return {
            "profile_id": profile_id,
            "job_id": job_id,
            "current": versions[-1] if versions else None,
            "versions": versions,
        }

    @router.put("/profiles/{profile_id}/jobs/{job_id}/resume-workspace")
    def put_resume_workspace(profile_id: str, job_id: str, value: ResumeWorkspaceSaveInput):
        sections = [row.model_dump() for row in value.sections]
        try:
            saved = save_row(
                store,
                profile_id=profile_id,
                job_id=job_id,
                expected_version=value.expected_version,
                profile_version=value.profile_version,
                job_fingerprint=value.job_fingerprint,
                template_id=value.template_id,
                sections=sections,
            )
        except Exception as error:
            raise _service_http_error(error) from None
        result = _api_row(saved, profile_id=profile_id, job_id=job_id)
        expected_version = (value.expected_version or 0) + 1
        if (result["version"] != expected_version
                or result["profile_version"] != value.profile_version
                or result["job_fingerprint"] != value.job_fingerprint
                or result["template_id"] != value.template_id
                or result["sections"] != sections):
            raise HTTPException(500, {"code": "RESUME_WORKSPACE_READBACK_INVALID"})
        return result

    return router
