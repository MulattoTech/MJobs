"""Resolve the effective Jobs view before a caller applies its result limit."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from itertools import islice
from types import SimpleNamespace

from sqlalchemy import select

from packages.persistence import models as m
from services.discovery.search_profiles import _clean_job_filters, parse_utc_date_bound
from services.matching.company_preferences import list_company_preferences
from services.tracking.jobs import list_job_tracking


_TEXT_FILTERS = ("q", "title", "company", "location")
_OPTIONAL_FILTERS = (
    "remote_type", "provider", "experience_level", "employment_type", "education_requirement",
    "clearance_level", "job_source_id", "ats_provider", "application_link_status",
    "application_pipeline_status", "application_capability",
)
_WINDOWS = {
    "LAST_HOUR": timedelta(hours=1),
    "LAST_24_HOURS": timedelta(days=1),
    "LAST_3_DAYS": timedelta(days=3),
    "LAST_7_DAYS": timedelta(days=7),
    "LAST_30_DAYS": timedelta(days=30),
}


def _date_bounds(criteria, date_overrides, legacy_days, at):
    window = date_overrides.get("date_window", criteria.get("date_window", "ANY"))
    if date_overrides.get("posted_within_days") is not None:
        return at - timedelta(days=date_overrides["posted_within_days"]), at
    if window == "CUSTOM":
        source = date_overrides if date_overrides else criteria
        return parse_utc_date_bound(source.get("date_start")), parse_utc_date_bound(source.get("date_end"))
    if window == "TODAY":
        return at.replace(hour=0, minute=0, second=0, microsecond=0), at
    if window in _WINDOWS:
        return at - _WINDOWS[window], at
    if legacy_days and not date_overrides:
        return at - timedelta(days=int(legacy_days)), at
    return None, None


def resolve_jobs_query(store, profile_id: str | None, criteria: dict | None, *, overrides=None, date_overrides=None,
                       replace_saved_facets=False, override_saved_score=False, advisory=False,
                       connection=None, at=None):
    """Return Store options and the effective saved score floor for one owned view."""
    if criteria is not None and not isinstance(criteria, dict):
        raise ValueError("Saved search criteria must be an object")
    at = at if at is not None else datetime.now(timezone.utc)
    overrides = overrides or {}
    date_overrides = date_overrides or {}
    saved = _clean_job_filters((criteria or {}).get("job_filters"))
    basis = (saved.get("arrangement_basis", "STORED") if saved is not None
             else "EVALUATED" if criteria is not None else "STORED")
    filters = {} if replace_saved_facets else (saved or {})
    options = {key: filters.get(key, "") for key in _TEXT_FILTERS}
    options.update({key: filters.get(key) or None for key in _OPTIONAL_FILTERS})
    for key in ("salary_min", "salary_max"):
        options[key] = float(filters[key]) if filters.get(key) else None
    options["salary_period"] = filters.get("salary_period", "YEAR")
    salary_policy = filters.get("salary_policy", "DEFAULT")
    explicit_salary_policy = "salary_known_only" in overrides or "salary_unknown_allowed" in overrides
    options["salary_known_only"] = not explicit_salary_policy and salary_policy == "KNOWN_ONLY"
    options["salary_unknown_allowed"] = True if not explicit_salary_policy and salary_policy == "ALLOW_UNKNOWN" else None
    options["skill_id"] = filters.get("skill") or None
    options.update(overrides)
    for key in _OPTIONAL_FILTERS:
        options[key] = options[key] or None
    if options["job_source_id"]:
        with store.engine.connect() if connection is None else nullcontext(connection) as source_connection:
            source = source_connection.scalar(select(m.job_sources.c.id).where(
                m.job_sources.c.id == options["job_source_id"],
            ))
        if source is None:
            raise ValueError("Job source is not configured")
    saved_minimum = filters.get("min_match_score")
    minimum = options.pop("min_match_score", None)
    if saved_minimum and not override_saved_score:
        minimum = max(minimum or 0, int(saved_minimum))
    if not advisory:
        options["min_match_score"] = minimum

    saved_visibility = saved or {}
    include_dismissed = options.pop("include_dismissed", saved_visibility.get("include_dismissed", False))
    include_blacklisted = options.pop("include_blacklisted", saved_visibility.get("include_blacklisted", False))
    if advisory:
        include_dismissed = include_blacklisted = False
    # The existing visibility readers only need engine.connect(). On an owned
    # snapshot, borrow that read connection without returning it to the pool.
    visibility_store = store if connection is None else SimpleNamespace(
        engine=SimpleNamespace(connect=lambda: nullcontext(connection)),
    )
    if profile_id and not include_blacklisted:
        preferences = list_company_preferences(visibility_store, profile_id)
        options["exclude_company_ids"] = [row["company_id"] for row in preferences
                                          if row["status"] == "BLACKLISTED"]
    if profile_id and not include_dismissed:
        def visible(rows):
            while batch := list(islice(rows, 100)):
                ids = [row["id"] for row in batch]
                tracking = list_job_tracking(visibility_store, profile_id, ids)
                for row in batch:
                    row["tracking"] = tracking[row["id"]]
                    if row["tracking"]["disposition"] not in ("HIDDEN", "NOT_INTERESTED"):
                        yield row
        options["row_filter"] = visible

    posted_after, posted_before = _date_bounds(criteria or {}, date_overrides, filters.get("posted_within_days"), at)
    semantic = criteria
    if semantic is not None and date_overrides:
        semantic = {**semantic, "date_window": "ANY", "date_start": None, "date_end": None}
    options.update(search_criteria=semantic, arrangement_basis=basis, search_at=at,
                   posted_after=posted_after, posted_before=posted_before,
                   exclude_closed=criteria is not None)
    return options, minimum
