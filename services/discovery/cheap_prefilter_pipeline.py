"""Apply an existing search profile's cheap filter before deterministic job filtering."""

from services.discovery.cheap_prefilter import prefilter_postings


def prefilter_search_profile_postings(postings, search_profile, *, max_candidates=500):
    """Partition captured postings using the profile criteria, retaining every input row.

    The returned ``candidates`` are the only rows that should continue to the
    existing ``search_semantics.filter_jobs`` path. Rejected and deferred rows
    remain explicit outputs so this candidate-work optimization never implies
    that a captured source posting was deleted.
    """
    if not isinstance(search_profile, dict):
        raise ValueError("Search profile must be an object")
    criteria = search_profile.get("criteria")
    if not isinstance(criteria, dict):
        raise ValueError("Search profile criteria must be an object")

    result = prefilter_postings(postings, criteria, max_candidates=max_candidates)
    candidates = result["candidates"]
    rejected = result["rejected"]
    deferred = result["deferred"]
    stage_counts = result["stage_counts"]
    return {
        "candidates": candidates,
        "rejected": rejected,
        "deferred": deferred,
        "stage_counts": {
            "captured": stage_counts["captured"],
            "prefiltered": stage_counts["prefiltered"],
        },
        "candidate_count": len(candidates),
        "rejected_count": len(rejected),
        "deferred_count": len(deferred),
    }
