"""Profile/job-scoped preview for already captured browser job fields.

This adapter never opens a browser or fetches a page. Captured page content is
untrusted input to a deterministic local extraction service and is not persisted.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m


_MAX_CAPTURE_BYTES = 250_000
_FIELD_NAMES = (
    "title", "company", "description", "location", "employment_type",
    "date_posted", "valid_through", "source_job_id", "listing_url",
    "application_url",
)
_SOURCES = frozenset({
    "JOBPOSTING_SCHEMA", "PROVIDER_FIELDS", "USER_SELECTED_TEXT", "VISIBLE_TEXT", "UNKNOWN",
})


class BrowserJobCaptureInput(BaseModel):
    """Allowlisted data already captured locally by the browser/user."""

    model_config = ConfigDict(strict=True, extra="forbid")

    raw_capture_ref: str | None = Field(default=None, max_length=256)
    job_posting: dict[str, Any] | str | None = None
    provider: str | None = Field(default=None, max_length=64)
    provider_fields: dict[str, Any] | None = None
    selected_text: str | None = Field(default=None, max_length=50_000)
    visible_text: str | None = Field(default=None, max_length=100_000)

    @field_validator("raw_capture_ref")
    @classmethod
    def normalize_capture_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("Capture reference must be a nonblank opaque string")
        return normalized

    @model_validator(mode="after")
    def captured_data_is_present_and_bounded(self):
        if not any(value is not None for value in (
                self.job_posting, self.provider_fields, self.selected_text, self.visible_text)):
            raise ValueError("At least one captured job field source is required")
        try:
            encoded = json.dumps(
                self.model_dump(exclude_none=True), ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("Captured job data must be JSON-compatible") from None
        if len(encoded) > _MAX_CAPTURE_BYTES:
            raise ValueError("Captured job data exceeds the preview size limit")
        return self


class BrowserJobExtractionPreviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    capture: BrowserJobCaptureInput


def _default_extractor(capture: dict[str, Any]) -> dict[str, Any]:
    # Lazy import keeps this API adapter composable while Luke's service is integrated.
    from services.applications.job_extraction import extract_job_capture

    return extract_job_capture(capture)


def _profile(store, profile_id: str) -> dict:
    try:
        return store.profile(profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        raise HTTPException(
            404, {"code": "BROWSER_JOB_EXTRACTION_PROFILE_NOT_FOUND"},
        ) from None


def _require_profile_job_match(store, profile_id: str, profile_version: str, job_id: str) -> None:
    with store.engine.connect() as connection:
        match = connection.execute(select(m.matches.c.job_id).join(
            m.jobs, m.jobs.c.id == m.matches.c.job_id,
        ).where(
            m.matches.c.profile_id == profile_id,
            m.matches.c.profile_version == profile_version,
            m.matches.c.job_id == job_id,
        )).scalar_one_or_none()
    if match is None:
        raise HTTPException(
            404, {"code": "BROWSER_JOB_EXTRACTION_JOB_NOT_FOUND"},
        )


def _validated_result(result: Any, expected_capture_ref: str | None) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != {"fields", "raw_capture_ref", "review_required"}:
        raise ValueError("Extraction result envelope is invalid")
    if result["raw_capture_ref"] != expected_capture_ref:
        raise ValueError("Extraction capture reference does not match the request")
    values = result["fields"]
    if not isinstance(values, dict) or set(values) != set(_FIELD_NAMES):
        raise ValueError("Extraction result fields are invalid")

    fields: dict[str, dict[str, Any]] = {}
    unknown_fields: list[str] = []
    review_required: list[str] = []
    for name in _FIELD_NAMES:
        item = values[name]
        if not isinstance(item, dict) or set(item) != {
            "value", "source", "confidence", "review_required",
        }:
            raise ValueError("Extraction field result is invalid")
        value = item["value"]
        source = item["source"]
        confidence = item["confidence"]
        needs_review = item["review_required"]
        if value is not None and (not isinstance(value, str) or len(value) > 50_000):
            raise ValueError("Extracted field value is invalid")
        if source not in _SOURCES:
            raise ValueError("Extracted field source is invalid")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValueError("Extracted field confidence is invalid")
        if type(needs_review) is not bool:
            raise ValueError("Extracted field review status is invalid")
        if value is None:
            if source != "UNKNOWN" or confidence != 0 or needs_review:
                raise ValueError("Unknown extracted fields must be explicit and empty")
            unknown_fields.append(name)
        elif source == "UNKNOWN":
            raise ValueError("Known extracted fields require a provenance source")
        if needs_review:
            review_required.append(name)
        fields[name] = {
            "value": value,
            "source": source,
            "confidence": float(confidence),
            "review_required": needs_review,
        }

    if result["review_required"] != review_required:
        raise ValueError("Aggregate review list does not match field review flags")
    return {
        "raw_capture_ref": expected_capture_ref,
        "fields": fields,
        "unknown_fields": unknown_fields,
        "review_required": review_required,
    }


def create_browser_job_extraction_router(
    store, *, extractor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> APIRouter:
    """Create a read-only preview route over an existing profile/job pair."""
    router = APIRouter()
    run_extraction = extractor or _default_extractor

    @router.post("/profiles/{profile_id}/jobs/{job_id}/browser-extraction-preview")
    def browser_job_extraction_preview(
        profile_id: str,
        job_id: str,
        value: BrowserJobExtractionPreviewInput,
    ):
        profile = _profile(store, profile_id)
        _require_profile_job_match(store, profile_id, profile["version"], job_id)
        capture = value.capture.model_dump(exclude_none=True)
        try:
            extracted = run_extraction(capture)
        except (TypeError, ValueError) as error:
            raise HTTPException(
                422, {"code": "BROWSER_JOB_EXTRACTION_CAPTURE_INVALID", "detail": str(error)},
            ) from None
        try:
            result = _validated_result(extracted, capture.get("raw_capture_ref"))
        except (TypeError, ValueError) as error:
            raise HTTPException(
                502, {"code": "BROWSER_JOB_EXTRACTION_RESULT_INVALID", "detail": str(error)},
            ) from None
        return {"profile_id": profile_id, "job_id": job_id, **result}

    return router
