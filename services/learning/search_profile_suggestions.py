"""Suggest related job titles from current strong matches.

These suggestions are informational. Callers must not use them to widen saved
searches or auto-apply criteria without a separate user action.
"""
from math import isfinite

from sqlalchemy import select

from packages.persistence import models as m


# Keep this aligned with apps/web/src/match-category.ts, where Strong begins
# at 75 and the higher Exceptional/Excellent categories occupy the same range.
_STRONG_CATEGORY_MINIMUM = 75
_MATCH_SCORE_MAXIMUM = 100
_MAX_SUGGESTIONS = 20


def _has_strong_match_category(score):
    """Return whether a persisted score maps to an existing strong category."""
    if type(score) not in (int, float) or not isfinite(score):
        return False
    return _STRONG_CATEGORY_MINIMUM <= score <= _MATCH_SCORE_MAXIMUM


def suggest_search_profile_titles(store, profile_id, current_title=None):
    """Aggregate titles from current-version strong matches for one profile.

    Results are case-insensitively deduplicated, ordered by descending count
    then normalized title, and capped at 20. strong_match_count counts
    current-profile/current-version scored jobs in the existing Strong,
    Excellent, or Exceptional categories.
    """
    with store.engine.connect() as connection:
        profile_version = connection.execute(
            select(m.profiles.c.version).where(m.profiles.c.id == profile_id)
        ).scalar_one_or_none()
        if profile_version is None:
            return []

        rows = connection.execute(
            select(m.jobs.c.title, m.matches.c.score)
            .select_from(m.matches.join(m.jobs, m.jobs.c.id == m.matches.c.job_id))
            .where(
                m.matches.c.profile_id == profile_id,
                m.matches.c.profile_version == profile_version,
                m.matches.c.score.is_not(None),
            )
        ).all()

    excluded_title = current_title.strip().casefold() if isinstance(current_title, str) else ""
    grouped = {}
    for title, score in rows:
        if not isinstance(title, str):
            continue
        title = title.strip()
        normalized = title.casefold()
        if not normalized or normalized == excluded_title or not _has_strong_match_category(score):
            continue

        entry = grouped.setdefault(normalized, {"title": title, "strong_match_count": 0})
        if (title.casefold(), title) < (entry["title"].casefold(), entry["title"]):
            entry["title"] = title
        entry["strong_match_count"] += 1

    suggestions = []
    for item in sorted(
        grouped.values(),
        key=lambda value: (
            -value["strong_match_count"],
            value["title"].casefold(),
            value["title"],
        ),
    )[:_MAX_SUGGESTIONS]:
        count = item["strong_match_count"]
        noun = "match" if count == 1 else "matches"
        suggestions.append({
            **item,
            "reason": f"Observed in {count} current strong {noun}.",
        })
    return suggestions


__all__ = ["suggest_search_profile_titles"]
