"""Profile-scoped downloadable data exports."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import NoResultFound


_FILENAMES = {
    "jobs": "mjobs-jobs.csv",
    "applications": "mjobs-applications.csv",
    "profile": "mjobs-profile.json",
    "backup": "mjobs-profile-backup.json",
}


def _profile_exists(store, profile_id: str) -> None:
    try:
        store.profile(profile_id)
    except NoResultFound:
        raise HTTPException(
            status_code=404,
            detail={"code": "DATA_EXPORT_PROFILE_NOT_FOUND"},
        ) from None


def _service_functions():
    # Resolve lazily so the API module remains importable during service startup
    # and the service can be developed independently from the router.
    from services.data_export import (
        export_applications_csv,
        export_jobs_csv,
        export_profile_backup,
        export_profile_json,
    )
    return {
        "jobs": export_jobs_csv,
        "applications": export_applications_csv,
        "profile": export_profile_json,
        "backup": export_profile_backup,
    }


def _attachment(content: str, kind: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{_FILENAMES[kind]}"'},
    )


def _json_document(value, profile_id: str, kind: str) -> str:
    try:
        parsed = json.loads(value) if kind == "profile" and isinstance(value, str) else value
        if not isinstance(parsed, dict):
            raise ValueError
        profile = parsed.get("profile")
        if (parsed.get("profile_id") != profile_id
                or not isinstance(profile, dict)
                or profile.get("id") != profile_id):
            raise ValueError
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError):
        raise HTTPException(
            status_code=500,
            detail={"code": "DATA_EXPORT_RESPONSE_INVALID"},
        ) from None


def create_data_export_router(store, *, output_dir=None) -> APIRouter:
    """Create profile-authenticated CSV, JSON, and complete-backup downloads."""
    router = APIRouter()

    def export_text(profile_id: str, kind: str):
        _profile_exists(store, profile_id)
        try:
            content = _service_functions()[kind](store, profile_id)
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "DATA_EXPORT_PROFILE_NOT_FOUND"},
            ) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=500,
                detail={"code": "DATA_EXPORT_FAILED"},
            ) from None
        if not isinstance(content, str):
            raise HTTPException(
                status_code=500,
                detail={"code": "DATA_EXPORT_RESPONSE_INVALID"},
            )
        if kind in {"jobs", "applications"}:
            return _attachment(content, kind, "text/csv; charset=utf-8")
        normalized = _json_document(content, profile_id, kind)
        return _attachment(normalized, kind, "application/json")

    @router.get("/profiles/{profile_id}/exports/jobs.csv")
    def export_jobs(profile_id: str):
        return export_text(profile_id, "jobs")

    @router.get("/profiles/{profile_id}/exports/applications.csv")
    def export_applications(profile_id: str):
        return export_text(profile_id, "applications")

    @router.get("/profiles/{profile_id}/exports/profile.json")
    def export_profile(profile_id: str):
        return export_text(profile_id, "profile")

    @router.get("/profiles/{profile_id}/exports/backup.json")
    def export_backup(profile_id: str):
        _profile_exists(store, profile_id)
        try:
            content = _service_functions()["backup"](
                store, profile_id, output_dir=output_dir,
            )
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "DATA_EXPORT_PROFILE_NOT_FOUND"},
            ) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=500,
                detail={"code": "DATA_EXPORT_FAILED"},
            ) from None
        normalized = _json_document(content, profile_id, "backup")
        return _attachment(normalized, "backup", "application/json")

    return router
