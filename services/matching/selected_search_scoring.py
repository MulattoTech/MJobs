"""Compose match scoring from the explicitly selected saved-search criteria."""

from collections.abc import Mapping

from services.matching.profile_scoring import resolve_and_compose_profile_scoring


_SCORING_PROFILE_KEY = "role_scoring_profile"


def compose_selected_search_match(job, legacy_match, criteria):
    """Apply only the scoring profile present in one selected search's criteria.

    The input is the criteria object loaded for the selected saved search. This
    adapter neither looks up other searches nor mutates the criteria. Missing
    configuration preserves the legacy match score; configured data is sent
    through the shared profile resolver and scorer for validation and attribution.
    """
    if not isinstance(job, Mapping):
        raise ValueError("MATCH_JOB_INVALID")
    if not isinstance(criteria, Mapping):
        raise ValueError("SEARCH_PROFILE_CRITERIA_INVALID")
    # SQLAlchemy RowMapping implements Mapping but specialized signal validation
    # intentionally accepts plain dictionaries only. Normalize at this boundary.
    job = dict(job)

    profile = criteria.get(_SCORING_PROFILE_KEY)
    if profile is None:
        return resolve_and_compose_profile_scoring(
            job,
            legacy_match,
            role_family=None,
            profiles_by_role_family={},
        )
    if not isinstance(profile, Mapping):
        raise ValueError("ROLE_SCORING_PROFILE_INVALID")

    role_family = profile.get("role_family")
    if not isinstance(role_family, str):
        raise ValueError("ROLE_SCORING_PROFILE_INVALID")

    return resolve_and_compose_profile_scoring(
        job,
        legacy_match,
        role_family=role_family,
        profiles_by_role_family={role_family: profile},
    )
