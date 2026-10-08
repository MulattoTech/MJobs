"""Profile-scoped, read-only preview route for cheap title/keyword filtering."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.discovery.search_profiles import get_search_profile


_MAX_POSTINGS = 100_000
_MAX_TERMS = 100
_MAX_CANDIDATES = 500
_Term = Annotated[str, Field(min_length=1, max_length=200)]


class CheapPrefilterCriteriaInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    include_titles: list[_Term] = Field(default_factory=list, max_length=_MAX_TERMS)
    exclude_titles: list[_Term] = Field(default_factory=list, max_length=_MAX_TERMS)
    required_keywords: list[_Term] = Field(default_factory=list, max_length=_MAX_TERMS)
    excluded_keywords: list[_Term] = Field(default_factory=list, max_length=_MAX_TERMS)
    exact_phrases: list[_Term] = Field(default_factory=list, max_length=_MAX_TERMS)

    @field_validator(
        "include_titles", "exclude_titles", "required_keywords",
        "excluded_keywords", "exact_phrases",
    )
    @classmethod
    def terms_must_be_nonblank(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Prefilter terms cannot be blank")
        return [value.strip() for value in values]


class CheapPrefilterPreviewInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    criteria: CheapPrefilterCriteriaInput


def _selected_source_postings(store, source_ids: list[str]) -> list[dict]:
    """Read original, unscored source rows for the saved search's explicit sources."""
    if not isinstance(source_ids, list):
        raise HTTPException(422, {"code": "CHEAP_PREFILTER_SOURCE_SCOPE_UNSUPPORTED"})
    if not source_ids:
        raise HTTPException(422, {"code": "CHEAP_PREFILTER_SOURCES_REQUIRED"})
    if any(not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids):
        raise HTTPException(422, {"code": "CHEAP_PREFILTER_SOURCE_SCOPE_UNSUPPORTED"})
    with store.engine.connect() as connection:
        available = set(connection.execute(select(m.job_sources.c.id).where(
            m.job_sources.c.id.in_(source_ids),
        )).scalars())
    if available != set(source_ids):
        raise HTTPException(422, {"code": "CHEAP_PREFILTER_SOURCES_UNAVAILABLE"})
    statement = select(m.postings.c.raw).where(
        m.postings.c.source.in_(source_ids),
    ).order_by(m.postings.c.id).limit(_MAX_POSTINGS + 1)
    with store.engine.connect() as connection:
        return [dict(row[0]) if isinstance(row[0], dict) else row[0]
                for row in connection.execute(statement)]


def create_cheap_prefilter_router(store) -> APIRouter:
    """Preview saved-search title/keyword criteria without changing stored postings."""
    router = APIRouter()

    @router.post("/profiles/{profile_id}/search-profiles/{search_profile_id}/cheap-prefilter-preview")
    def cheap_prefilter_preview(
        profile_id: str,
        search_profile_id: str,
        value: CheapPrefilterPreviewInput,
    ):
        try:
            store.profile(profile_id)
        except NoResultFound:
            raise HTTPException(404, {"code": "CHEAP_PREFILTER_PROFILE_NOT_FOUND"}) from None
        try:
            search_profile = get_search_profile(store, search_profile_id)
        except NoResultFound:
            raise HTTPException(404, {"code": "CHEAP_PREFILTER_SEARCH_PROFILE_NOT_FOUND"}) from None
        if search_profile["profile_id"] != profile_id:
            raise HTTPException(409, {"code": "CHEAP_PREFILTER_SEARCH_PROFILE_PROFILE_MISMATCH"})

        source_ids = search_profile.get("source_ids") or []
        postings = _selected_source_postings(store, source_ids)
        criteria = dict(search_profile.get("criteria") or {})
        criteria.update(value.criteria.model_dump(exclude_unset=True))
        preview_profile = {**search_profile, "criteria": criteria}

        try:
            from services.discovery.cheap_prefilter_pipeline import prefilter_search_profile_postings

            result = prefilter_search_profile_postings(
                postings, preview_profile, max_candidates=_MAX_CANDIDATES,
            )
        except ValueError as error:
            raise HTTPException(422, {"code": "CHEAP_PREFILTER_REQUEST_INVALID", "detail": str(error)}) from None

        return {
            "profile_id": profile_id,
            "search_profile_id": search_profile_id,
            "stage_counts": result["stage_counts"],
            "candidate_count": result["candidate_count"],
            "rejected_count": result["rejected_count"],
            "deferred_count": result["deferred_count"],
        }

    return router
