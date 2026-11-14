"""Profile-scoped user reports about exact resume versions and observed outcomes."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import NoResultFound

from services.tracking.resume_outcomes import (
    ResumeOutcomeConflict,
    analyze_resume_outcomes,
    record_observed_outcome,
    report_submitted_resume_version,
)


class ResumeVersionReportInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    document_id: str = Field(min_length=1, max_length=128)
    reported_at: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)


class ResumeOutcomeEventInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["RESPONSE", "SCREEN", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"]
    observed_at: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)


def _raise_service_error(error: Exception):
    if isinstance(error, ResumeOutcomeConflict):
        raise HTTPException(409, {"code": "RESUME_OUTCOME_CONFLICT"}) from None
    if isinstance(error, (NoResultFound, LookupError)):
        raise HTTPException(404, {"code": "RESUME_OUTCOME_NOT_FOUND"}) from None
    if isinstance(error, ValueError):
        raise HTTPException(422, {"code": "RESUME_OUTCOME_INVALID"}) from None
    raise error


def create_resume_outcomes_router(store) -> APIRouter:
    router = APIRouter()

    @router.post("/profiles/{profile_id}/applications/{application_id}/resume-outcomes/binding")
    def report_version(profile_id: str, application_id: str, value: ResumeVersionReportInput):
        """Record a user's version report; this does not submit an application."""
        try:
            return report_submitted_resume_version(
                store, profile_id, application_id, value.document_id,
                value.reported_at, value.request_id,
            )
        except (NoResultFound, LookupError, ValueError) as error:
            _raise_service_error(error)

    @router.post("/profiles/{profile_id}/applications/{application_id}/resume-outcomes/events")
    def report_event(profile_id: str, application_id: str, value: ResumeOutcomeEventInput):
        """Append a user-observed milestone for a previously reported version."""
        try:
            return record_observed_outcome(
                store, profile_id, application_id, value.kind,
                value.observed_at, value.request_id,
            )
        except (NoResultFound, LookupError, ValueError) as error:
            _raise_service_error(error)

    @router.get("/profiles/{profile_id}/resume-outcomes")
    def read_analysis(profile_id: str,
                      since: str | None = Query(default=None, max_length=64),
                      until: str | None = Query(default=None, max_length=64)):
        """Return observed counts and unknowns; rates are not causal estimates."""
        try:
            return analyze_resume_outcomes(store, profile_id, since, until)
        except (NoResultFound, LookupError, ValueError) as error:
            _raise_service_error(error)

    return router
