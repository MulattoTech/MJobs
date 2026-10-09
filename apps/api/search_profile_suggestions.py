"""Profile-scoped read-only suggestions for related search titles."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from services.learning.search_profile_suggestions import suggest_search_profile_titles

_MAX_SUGGESTIONS = 20


def _rank_suggestions(suggestions):
    """Bound and rank read-only suggestions by current evidence count."""
    ranked = sorted(
        suggestions,
        key=lambda item: (
            -item["strong_match_count"],
            item["title"].casefold(),
            item["title"],
        ),
    )[:_MAX_SUGGESTIONS]
    return [
        {
            "title": item["title"],
            "strong_match_count": item["strong_match_count"],
            "reason": (
                f"Observed in {item['strong_match_count']} current strong match"
                f"{'es' if item['strong_match_count'] != 1 else ''}."
            ),
        }
        for item in ranked
    ]


def create_search_profile_suggestions_router(store) -> APIRouter:
    """Build the read-only related-title suggestions endpoint."""
    router = APIRouter()

    @router.get("/profiles/{profile_id}/search-profile-suggestions")
    def search_profile_suggestions(
        profile_id: str,
        current_title: str | None = Query(default=None, max_length=500),
        search_profile_id: str | None = None,
    ):
        try:
            profile = store.profile(profile_id)
            excluded_titles = ()
            if search_profile_id is not None:
                with store.engine.connect() as connection:
                    search_profile = connection.execute(select(m.search_profiles).where(
                        m.search_profiles.c.id == search_profile_id,
                        m.search_profiles.c.profile_id == profile_id,
                    )).mappings().one_or_none()
                if search_profile is None:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "SEARCH_PROFILE_SUGGESTIONS_SEARCH_NOT_FOUND"},
                    )
                criteria = search_profile["criteria"]
                excluded_titles = criteria.get("include_titles", []) if isinstance(criteria, dict) else []
            suggestions = _rank_suggestions(suggest_search_profile_titles(
                store, profile_id, current_title=current_title, excluded_titles=excluded_titles,
            ))
        except HTTPException:
            raise
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail={"code": "SEARCH_PROFILE_SUGGESTIONS_PROFILE_NOT_FOUND"},
            ) from None
        return {
            "profile_id": profile_id,
            "profile_version": profile["version"],
            "current_title": current_title.strip() if current_title and current_title.strip() else None,
            "search_profile_id": search_profile_id,
            "suggestions": suggestions,
        }

    return router
