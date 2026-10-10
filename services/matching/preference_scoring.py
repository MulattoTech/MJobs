"""Weight deterministic personal-fit evidence without trusting posting fit scores.

Native preference dimensions are reserved: a job's specialized_signals cannot
supply a candidate's preference or location. The canonical comparison builder
is reused, rather than duplicating its title, currency and region rules.
"""
from collections.abc import Mapping

from services.discovery.search_semantics import EMPLOYMENT_TYPES, REMOTE_TYPES

PREFERENCE_DIMENSIONS = frozenset({
    "title", "salary", "work_arrangement", "employment_type", "remote_location",
})
_LABELS = {
    "title": "Title preference", "salary": "Annual USD salary overlap",
    "work_arrangement": "Work arrangement", "employment_type": "Employment type",
    "remote_location": "Remote work region",
}


def _text_values(value):
    return (isinstance(value, (list, tuple, set)) and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value))


def _supported_inputs(dimension, job, profile):
    """Do not turn malformed or unsupported classifications into zero-score facts."""
    preferences = profile.get("preferences") or {}
    attributes = job.get("attributes") or {}
    if dimension == "title":
        return isinstance(job.get("title"), str) and _text_values(preferences.get("desired_roles"))
    if dimension == "employment_type":
        wanted = preferences.get("employment_types")
        known = EMPLOYMENT_TYPES - {"UNKNOWN"}
        return (_text_values(wanted) and all(value in known for value in wanted)
                and isinstance(attributes.get("employment_type"), str)
                and attributes["employment_type"] in known)
    if dimension in {"work_arrangement", "remote_location"}:
        remote_type = job.get("remote_type") or "UNKNOWN"
        if not isinstance(remote_type, str) or remote_type not in REMOTE_TYPES | {"REMOTE", "ON_SITE"}:
            return False
        if dimension == "work_arrangement":
            wanted = preferences.get("work_arrangements")
            return (_text_values(wanted) and all(value in {"REMOTE", "HYBRID", "ON_SITE", "ONSITE"}
                                                for value in wanted))
        return _text_values(attributes.get("remote_regions"))
    return True  # Salary's canonical comparator already validates currency/source/bounds.


def preference_contributions(job, candidate_profile, weights):
    """Return raw 0/100/null contributions; the composer normalizes their weights.

    UNKNOWN is not zero and is excluded from the denominator. A zero is only a
    supported direct preference mismatch, never a missing qualification. Text,
    salary bounds, address values and posting-supplied provenance are not echoed.
    """
    configured = {key: weight for key, weight in weights.items()
                  if key in PREFERENCE_DIMENSIONS and weight > 0}
    if not configured:
        return []
    factors = []
    if (isinstance(job, Mapping) and isinstance(candidate_profile, Mapping)
            and isinstance(candidate_profile.get("preferences") or {}, Mapping)
            and isinstance(candidate_profile.get("contact") or {}, Mapping)
            and isinstance(job.get("attributes") or {}, Mapping)):
        # Imported lazily to keep unprofiled scoring independent of this optional
        # path and use exactly the same evidence rules as the base explanation.
        from services.discovery.matching import build_preference_fit_factors

        try:
            factors = build_preference_fit_factors(dict(job), dict(candidate_profile))
            factors = [factor for factor in factors
                       if _supported_inputs(factor["id"], job, candidate_profile)]
        except (TypeError, ValueError, AttributeError):
            # A malformed optional metadata object supplies no personal-fit evidence.
            factors = []
    by_id = {factor["id"]: factor for factor in factors}
    result = []
    for dimension, weight in sorted(configured.items()):
        status = by_id.get(dimension, {}).get("status", "UNKNOWN")
        raw_score = {"ALIGNS": 100.0, "NO_DIRECT_MATCH": 0.0}.get(status)
        label = _LABELS[dimension]
        if raw_score is None:
            explanation = (
                f"{label} is UNKNOWN because a supported profile/job comparison is unavailable. "
                "It is excluded from the advisory-score denominator; no mismatch is inferred."
            )
            provenance = {"source": "UNKNOWN"}
        else:
            comparison = "aligns (100/100)" if raw_score else "has no direct match (0/100)"
            explanation = (
                f"{label} {comparison}. This configured weight affects the advisory score only; "
                "it does not change hard eligibility or establish candidate qualifications."
            )
            provenance = {
                "source": "DETERMINISTIC_PROFILE_JOB_FIT",
                "evidence_ref": f"match:fit_factors/{dimension}",
            }
        result.append({
            "dimension": dimension, "weight": weight, "raw_score": raw_score,
            "weighted_contribution": None, "explanation": explanation,
            "provenance": provenance,
        })
    return result
