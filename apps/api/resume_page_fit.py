"""Profile-scoped API routes for transient PDF page-fit previews."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import NoResultFound

from services.documents.errors import PreparationError
from services.documents.failure_status import DOCUMENT_GENERATION_FAILED
from services.documents.tailor import target_fingerprint


_PREVIEW_TTL_SECONDS = 15 * 60
_MAX_ACTIVE_PREVIEWS = 100


class PageFitPreviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    target_pdf_pages: int = Field(ge=1, le=20)
    resume_story_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    expected_profile_version: str | None = Field(default=None, min_length=64, max_length=64)
    expected_job_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def selected_resume_stories_require_snapshots(self):
        if self.resume_story_ids is not None and (
                len(set(self.resume_story_ids)) != len(self.resume_story_ids)
                or any(not item.strip() or len(item) > 128 for item in self.resume_story_ids)
                or not self.expected_profile_version or not self.expected_job_fingerprint):
            raise ValueError("Selected resume stories require unique IDs and observed profile and job versions")
        return self


class PageFitReviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    reviewed: bool


def _default_preview_runner(store, profile_id, job_id, output_dir, target_pdf_pages,
                            resume_story_ids=None, expected_profile_version=None,
                            expected_job_fingerprint=None):
    from services.documents.generate import preview_resume_page_fit

    return preview_resume_page_fit(
        store, profile_id, job_id, output_dir, target_pdf_pages,
        resume_story_ids=resume_story_ids,
        expected_profile_version=expected_profile_version,
        resume_story_job_fingerprint=expected_job_fingerprint,
    )


def _default_generate_runner(store, profile_id, job_id, output_dir, preview):
    from services.documents.generate import generate_resume_page_fit_preview

    return generate_resume_page_fit_preview(
        store, profile_id, job_id, output_dir, preview,
    )


def _page_fit_payload(result) -> dict:
    if is_dataclass(result):
        result = asdict(result)
    elif isinstance(result, dict) and isinstance(result.get("generation_context"), dict):
        result = result["generation_context"].get("page_fit", result)
    if not isinstance(result, dict):
        raise ValueError("page-fit preview result is unavailable")
    payload = {
        "page_target": result.get("page_target"),
        "page_count": result.get("page_count"),
        "attempts": result.get("attempts"),
        "measurement_available": result.get("measurement_available"),
        "target_met": result.get("target_met"),
        "reason": result.get("reason"),
        "omitted_facts": result.get("omitted_facts"),
    }
    if (type(payload["page_target"]) is not int or not 1 <= payload["page_target"] <= 20 or
            (payload["page_count"] is not None and
             (type(payload["page_count"]) is not int or payload["page_count"] < 1)) or
            type(payload["attempts"]) is not int or not 1 <= payload["attempts"] <= 10 or
            type(payload["measurement_available"]) is not bool or
            type(payload["target_met"]) is not bool or
            (payload["reason"] is not None and not isinstance(payload["reason"], str)) or
            not isinstance(payload["omitted_facts"], (list, tuple))):
        raise ValueError("page-fit preview result is malformed")
    omitted = []
    for item in payload["omitted_facts"]:
        if (not isinstance(item, dict) or set(item) != {"fact_id", "reason"} or
                not isinstance(item["fact_id"], str) or not item["fact_id"] or
                not isinstance(item["reason"], str) or
                item["reason"] != "PAGE_TARGET_FIT_LOWEST_RETENTION_RANK"):
            raise ValueError("page-fit omission provenance is malformed")
        omitted.append({"fact_id": item["fact_id"], "reason": item["reason"]})
    payload["omitted_facts"] = omitted
    if len({item["fact_id"] for item in omitted}) != len(omitted):
        raise ValueError("page-fit omission fact IDs must be unique")
    if payload["measurement_available"] and payload["page_count"] is None:
        raise ValueError("available page measurement must include its actual count")
    if payload["target_met"] and (not payload["measurement_available"] or
                                   payload["page_count"] is None or
                                   payload["page_count"] > payload["page_target"]):
        raise ValueError("page-fit success must match the measured page count")
    if not payload["target_met"] and omitted:
        raise ValueError("unmet page targets cannot apply omissions")
    return payload


def _load_profile(store, profile_id):
    try:
        return store.profile(profile_id)
    except NoResultFound:
        raise HTTPException(404, {"code": "RESUME_PAGE_FIT_PROFILE_NOT_FOUND"}) from None


def _load_job(store, job_id):
    try:
        return store.job(job_id)
    except NoResultFound:
        raise HTTPException(404, {"code": "RESUME_PAGE_FIT_JOB_NOT_FOUND"}) from None


def _contained_preview_pdf(output_dir, raw_path):
    """Resolve only a direct, non-symlink PDF child of the private preview dir."""
    if not isinstance(raw_path, (str, Path)):
        return None
    root = Path(output_dir).resolve() / ".page-fit-previews"
    if root.is_symlink():
        return None
    try:
        candidate = Path(raw_path)
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved.parent != root or resolved.suffix.casefold() != ".pdf":
        return None
    return resolved


def _cleanup_preview_file(output_dir, record):
    result = record.get("runner_result") if isinstance(record, dict) else None
    raw_path = result.get("preview_pdf_path") if isinstance(result, dict) else None
    path = _contained_preview_pdf(output_dir, raw_path)
    if path is None:
        return False
    try:
        if not path.is_file():
            return False
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True
    except OSError:
        return False


def create_resume_page_fit_router(store, output_dir: str | Path, *,
                                  preview_runner=None, generate_runner=None,
                                  clock=time.monotonic) -> APIRouter:
    """Create transient preview and explicitly reviewed generation endpoints.

    Preview records and PDF paths remain process-local and expire after fifteen
    minutes. A preview creates no application or document revision. Persisting
    one requires the profile-scoped generate route and ``reviewed: true``.
    """
    preview_runner = preview_runner or _default_preview_runner
    generate_runner = generate_runner or _default_generate_runner
    previews = {}
    lock = threading.Lock()
    router = APIRouter()

    def get_preview(profile_id, job_id, preview_id):
        expired = None
        with lock:
            record = previews.get(preview_id)
            if record is None:
                raise HTTPException(404, {"code": "RESUME_PAGE_FIT_PREVIEW_NOT_FOUND"})
            if clock() - record["created_at"] > _PREVIEW_TTL_SECONDS:
                expired = previews.pop(preview_id, None)
            elif record["profile_id"] != profile_id or record["job_id"] != job_id:
                raise HTTPException(404, {"code": "RESUME_PAGE_FIT_PREVIEW_NOT_FOUND"})
            else:
                return record
        if expired:
            _cleanup_preview_file(output_dir, expired)
            raise HTTPException(404, {"code": "RESUME_PAGE_FIT_PREVIEW_EXPIRED"})
        raise HTTPException(404, {"code": "RESUME_PAGE_FIT_PREVIEW_NOT_FOUND"})

    def discard_record(preview_id, record):
        with lock:
            if previews.get(preview_id) is record:
                previews.pop(preview_id, None)
        _cleanup_preview_file(output_dir, record)

    @router.post("/profiles/{profile_id}/jobs/{job_id}/resume-page-fit-previews")
    def preview_resume_page_fit(profile_id: str, job_id: str, value: PageFitPreviewInput):
        profile = _load_profile(store, profile_id)
        job = _load_job(store, job_id)
        if value.resume_story_ids is not None and (
                value.expected_profile_version != profile["version"]
                or value.expected_job_fingerprint != target_fingerprint(job)):
            raise HTTPException(409, {"code": "RESUME_PAGE_FIT_PREVIEW_STALE"})
        result = None
        try:
            if value.resume_story_ids is None:
                result = preview_runner(
                    store, profile_id, job_id, output_dir, value.target_pdf_pages,
                )
            else:
                result = preview_runner(
                    store, profile_id, job_id, output_dir, value.target_pdf_pages,
                    resume_story_ids=value.resume_story_ids,
                    expected_profile_version=value.expected_profile_version,
                    expected_job_fingerprint=value.expected_job_fingerprint,
                )
            page_fit = _page_fit_payload(
                result.get("page_fit", result.get("generation_context", {}).get("page_fit"))
                if isinstance(result, dict) else None
            )
            if page_fit["page_target"] != value.target_pdf_pages:
                raise ValueError("measured page target does not match the request")
        except HTTPException:
            if isinstance(result, dict):
                _cleanup_preview_file(output_dir, {"runner_result": result})
            raise
        except LookupError:
            if isinstance(result, dict):
                _cleanup_preview_file(output_dir, {"runner_result": result})
            raise HTTPException(404, {"code": "RESUME_PAGE_FIT_TARGET_NOT_FOUND"}) from None
        except PreparationError as error:
            if isinstance(result, dict):
                _cleanup_preview_file(output_dir, {"runner_result": result})
            if error.code == DOCUMENT_GENERATION_FAILED:
                raise HTTPException(503, {
                    "code": "RESUME_PAGE_FIT_MEASUREMENT_UNAVAILABLE",
                }) from None
            raise HTTPException(422, {"code": "RESUME_PAGE_FIT_PREVIEW_INVALID"}) from error
        except (ValueError, TypeError) as error:
            if isinstance(result, dict):
                _cleanup_preview_file(output_dir, {"runner_result": result})
            raise HTTPException(422, {"code": "RESUME_PAGE_FIT_PREVIEW_INVALID"}) from error
        except Exception:
            if isinstance(result, dict):
                _cleanup_preview_file(output_dir, {"runner_result": result})
            raise HTTPException(503, {"code": "RESUME_PAGE_FIT_MEASUREMENT_UNAVAILABLE"}) from None
        current_profile = _load_profile(store, profile_id)
        current_job = _load_job(store, job_id)
        if (current_profile["version"] != profile["version"] or
                target_fingerprint(current_job) != target_fingerprint(job)):
            _cleanup_preview_file(output_dir, {"runner_result": result})
            raise HTTPException(409, {"code": "RESUME_PAGE_FIT_PREVIEW_STALE"})

        preview_id = secrets.token_urlsafe(32)
        record = {
            "profile_id": profile_id,
            "job_id": job_id,
            "profile_version": profile["version"],
            "job_fingerprint": target_fingerprint(job),
            "created_at": clock(),
            "state": "READY",
            "page_fit": page_fit,
            "runner_result": result,
        }
        with lock:
            now = clock()
            expired = [key for key, item in previews.items()
                       if now - item["created_at"] > _PREVIEW_TTL_SECONDS]
            discarded = []
            for key in expired:
                item = previews.pop(key, None)
                if item is not None:
                    discarded.append(item)
            while len(previews) >= _MAX_ACTIVE_PREVIEWS:
                oldest_key = min(previews, key=lambda key: previews[key]["created_at"])
                discarded.append(previews.pop(oldest_key))
            previews[preview_id] = record
        for item in discarded:
            _cleanup_preview_file(output_dir, item)

        measured_unmet = (page_fit["measurement_available"] and
                          page_fit["page_count"] is not None and
                          page_fit["page_count"] > page_fit["page_target"])
        return {
            "profile_id": profile_id,
            "job_id": job_id,
            "preview_id": preview_id,
            "actual_page_count": page_fit["page_count"],
            "measurement_status": "AVAILABLE" if page_fit["measurement_available"] else "UNAVAILABLE",
            "unmet_target": measured_unmet,
            "preview_pdf_url": (
                f"/profiles/{profile_id}/jobs/{job_id}/resume-page-fit-previews/"
                f"{preview_id}/resume.pdf"
            ),
            "generation_context": {"page_fit": page_fit},
        }

    @router.get("/profiles/{profile_id}/jobs/{job_id}/resume-page-fit-previews/{preview_id}/resume.pdf")
    def read_resume_page_fit_preview(profile_id: str, job_id: str, preview_id: str):
        record = get_preview(profile_id, job_id, preview_id)
        raw_path = record["runner_result"].get("preview_pdf_path") if isinstance(
            record["runner_result"], dict) else None
        path = _contained_preview_pdf(output_dir, raw_path)
        if path is None or not path.is_file():
            raise HTTPException(404, {"code": "RESUME_PAGE_FIT_PREVIEW_FILE_NOT_FOUND"})
        return FileResponse(path, media_type="application/pdf", filename="resume-preview.pdf")

    @router.post("/profiles/{profile_id}/jobs/{job_id}/resume-page-fit-previews/{preview_id}/generate")
    def generate_page_fit_preview(profile_id: str, job_id: str, preview_id: str,
                                  value: PageFitReviewInput):
        if not value.reviewed:
            raise HTTPException(409, {"code": "RESUME_PAGE_FIT_REVIEW_REQUIRED"})
        record = get_preview(profile_id, job_id, preview_id)
        if not record["page_fit"]["measurement_available"]:
            raise HTTPException(409, {"code": "RESUME_PAGE_FIT_MEASUREMENT_UNAVAILABLE"})
        profile = _load_profile(store, profile_id)
        if profile["version"] != record["profile_version"]:
            discard_record(preview_id, record)
            raise HTTPException(409, {"code": "RESUME_PAGE_FIT_PREVIEW_STALE_PROFILE"})
        job = _load_job(store, job_id)
        if target_fingerprint(job) != record["job_fingerprint"]:
            discard_record(preview_id, record)
            raise HTTPException(409, {"code": "RESUME_PAGE_FIT_PREVIEW_STALE_JOB"})
        with lock:
            current = previews.get(preview_id)
            if current is not record or current.get("state") != "READY":
                raise HTTPException(409, {"code": "RESUME_PAGE_FIT_PREVIEW_ALREADY_CONSUMED"})
            current["state"] = "GENERATING"
        try:
            generated = generate_runner(
                store, profile_id, job_id, output_dir,
                {**record["runner_result"],
                 "page_fit": record["page_fit"],
                 "profile_version": record["profile_version"],
                 "job_fingerprint": record["job_fingerprint"]},
            )
        except LookupError:
            discard_record(preview_id, record)
            raise HTTPException(404, {"code": "RESUME_PAGE_FIT_TARGET_NOT_FOUND"}) from None
        except ValueError:
            discard_record(preview_id, record)
            raise HTTPException(409, {"code": "RESUME_PAGE_FIT_PREVIEW_STALE"}) from None
        except Exception:
            discard_record(preview_id, record)
            raise HTTPException(503, {"code": "RESUME_PAGE_FIT_GENERATION_UNAVAILABLE"}) from None
        if not isinstance(generated, dict) or not isinstance(generated.get("application_id"), str):
            discard_record(preview_id, record)
            raise HTTPException(503, {"code": "RESUME_PAGE_FIT_GENERATION_UNAVAILABLE"})
        discard_record(preview_id, record)
        return {
            "profile_id": profile_id,
            "job_id": job_id,
            "preview_id": preview_id,
            "application_id": generated["application_id"],
            "submit": False,
            "status": "PREPARED",
        }

    return router
