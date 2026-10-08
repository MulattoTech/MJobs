"""Persistent data-driven search profiles for one local MJobs workspace."""
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now, upsert
from services.discovery.search_semantics import (
    DATE_WINDOWS, EMPLOYMENT_TYPES, EXPERIENCE_LEVELS, SALARY_PERIODS, parse_timestamp,
)
from services.discovery.skill_taxonomy import normalize_skill_list
from services.discovery.requirement_parsers import CLEARANCE_LEVELS, WORK_AUTHORIZATION_FLAGS
from services.discovery.negative_filters import NEGATIVE_CONDITIONS


REMOTE_PREFERENCES = {"ANY", "REQUIRED", "EXCLUDED"}
WORK_ARRANGEMENTS = {"REMOTE", "HYBRID", "ONSITE"}
JOB_FILTERS = {"remote_type": {"", "REMOTE", "HYBRID", "ONSITE", "UNKNOWN"},
               "provider": {"", "mock-ats", "greenhouse"},
               "sort": {"score", "newest", "title"}}


def _clean_job_filters(value):
    if value is None:
        return None
    required = {"q", "remote_type", "provider", "sort"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"title", "company"}:
        raise ValueError("Saved Jobs filters must include q, remote_type, provider, and sort")
    query = value["q"]
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("Saved Jobs query must be text up to 200 characters")
    cleaned = {"q": query}
    for key in ("title", "company"):
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > 200:
            raise ValueError(f"Saved Jobs {key} must be text up to 200 characters")
        cleaned[key] = item
    for key, allowed in JOB_FILTERS.items():
        if not isinstance(value[key], str) or value[key] not in allowed:
            raise ValueError(f"Saved Jobs {key} is invalid")
        cleaned[key] = value[key]
    return cleaned


def _clean_strings(values, *, limit=100, length=200):
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError("Search profile list is invalid or too large")
    cleaned = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > length:
            raise ValueError("Search profile list contains an invalid value")
        item = value.strip()
        folded = item.casefold()
        if folded not in seen:
            cleaned.append(item)
            seen.add(folded)
    return cleaned


def _clean_aliases(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError("Technology aliases must be an object with at most 100 entries")
    result = {}
    for canonical, aliases in value.items():
        if not isinstance(canonical, str) or not canonical.strip() or len(canonical.strip()) > 100:
            raise ValueError("Technology alias canonical name is invalid")
        result[canonical.strip()] = _clean_strings(aliases, limit=30, length=100)
    return result


def _bounded_float(value, low, high, label):
    if value is None:
        return None
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"{label} is invalid")
    return float(value)


def save_search_profile(store, data):
    for key in ("id", "profile_id", "name"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError("Search profile identity, owner, and name are required")
    if len(data["id"]) > 128 or len(data["profile_id"]) > 128 or len(data["name"].strip()) > 200:
        raise ValueError("Search profile identity or name is too long")
    if type(data.get("enabled", True)) is not bool:
        raise ValueError("Search profile enabled must be boolean")
    job_filters = _clean_job_filters(data.get("job_filters"))
    if job_filters is not None and data.get("enabled", True):
        raise ValueError("Saved Jobs filters must be disabled for provider scheduling")

    remote = data.get("remote", "ANY")
    if remote not in REMOTE_PREFERENCES:
        raise ValueError("Search profile remote preference is invalid")
    employment = _clean_strings(data.get("employment_types", []), limit=20, length=32)
    if any(value not in EMPLOYMENT_TYPES for value in employment):
        raise ValueError("Search profile employment type is invalid")
    arrangements = _clean_strings(data.get("work_arrangements", []), limit=3, length=16)
    if any(value not in WORK_ARRANGEMENTS for value in arrangements):
        raise ValueError("Search profile work arrangement is invalid")
    experience = _clean_strings(data.get("experience_levels", []), limit=20, length=32)
    if any(value not in EXPERIENCE_LEVELS for value in experience):
        raise ValueError("Search profile experience level is invalid")
    salary_period = data.get("salary_period")
    if salary_period is not None and salary_period not in SALARY_PERIODS - {"UNKNOWN"}:
        raise ValueError("Search profile salary period is invalid")
    salary_min = _bounded_float(data.get("salary_min"), 0, 100000000, "Minimum salary")
    salary_max = _bounded_float(data.get("salary_max"), 0, 100000000, "Maximum salary")
    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        raise ValueError("Minimum salary cannot exceed maximum salary")
    if (salary_min is not None or salary_max is not None) and salary_period is None:
        raise ValueError("Salary filtering requires HOUR or YEAR period")
    if type(data.get("salary_known_only", False)) is not bool or type(data.get("salary_unknown_allowed", True)) is not bool:
        raise ValueError("Salary known/unknown policy must be boolean")
    date_window = data.get("date_window", "ANY")
    if date_window not in DATE_WINDOWS:
        raise ValueError("Search profile date window is invalid")
    date_start, date_end = data.get("date_start"), data.get("date_end")
    clearance_levels = _clean_strings(data.get("clearance_levels", []), limit=10, length=32)
    if any(value not in CLEARANCE_LEVELS for value in clearance_levels):
        raise ValueError("Search profile clearance level is invalid")
    if type(data.get("exclude_clearance_required", False)) is not bool:
        raise ValueError("Clearance exclusion policy must be boolean")
    excluded_auth = _clean_strings(data.get("excluded_work_authorization_flags", []), limit=20, length=64)
    if any(value not in WORK_AUTHORIZATION_FLAGS for value in excluded_auth):
        raise ValueError("Search profile work authorization flag is invalid")
    excluded_conditions = _clean_strings(data.get("excluded_conditions", []), limit=20, length=64)
    if any(value not in NEGATIVE_CONDITIONS for value in excluded_conditions):
        raise ValueError("Search profile negative condition is invalid")
    max_travel_percent = _bounded_float(data.get("max_travel_percent"), 0, 100, "Maximum travel percent")
    if date_window == "CUSTOM":
        start, end = parse_timestamp(date_start), parse_timestamp(date_end)
        if start is None or end is None or start > end:
            raise ValueError("Custom date filtering requires an ordered ISO start/end")
        date_start, date_end = start.isoformat(), end.isoformat()
    elif date_start is not None or date_end is not None:
        raise ValueError("Custom date bounds require date_window=CUSTOM")

    criteria = {
        "include_titles": _clean_strings(data.get("include_titles", [])),
        "exclude_titles": _clean_strings(data.get("exclude_titles", [])),
        "required_keywords": _clean_strings(data.get("required_keywords", [])),
        "optional_keywords": _clean_strings(data.get("optional_keywords", [])),
        "excluded_keywords": _clean_strings(data.get("excluded_keywords", [])),
        "exact_phrases": _clean_strings(data.get("exact_phrases", [])),
        "technology_aliases": _clean_aliases(data.get("technology_aliases", {})),
        "required_skills": normalize_skill_list(data.get("required_skills", [])),
        "optional_skills": normalize_skill_list(data.get("optional_skills", [])),
        "excluded_skills": normalize_skill_list(data.get("excluded_skills", [])),
        "company_include": _clean_strings(data.get("company_include", [])),
        "company_exclude": _clean_strings(data.get("company_exclude", [])),
        "remote": remote,
        "work_arrangements": arrangements,
        "countries": _clean_strings(data.get("countries", []), limit=50, length=100),
        "states": _clean_strings(data.get("states", []), limit=100, length=100),
        "cities": _clean_strings(data.get("cities", []), limit=100, length=200),
        "remote_regions": _clean_strings(data.get("remote_regions", []), limit=100, length=100),
        "employment_types": employment,
        "experience_levels": experience,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_period": salary_period,
        "salary_known_only": data.get("salary_known_only", False),
        "salary_unknown_allowed": data.get("salary_unknown_allowed", True),
        "date_window": date_window,
        "date_start": date_start,
        "date_end": date_end,
        "clearance_levels": clearance_levels,
        "exclude_clearance_required": data.get("exclude_clearance_required", False),
        "excluded_work_authorization_flags": excluded_auth,
        "excluded_conditions": excluded_conditions,
        "max_travel_percent": max_travel_percent,
        "radius_miles": _bounded_float(data.get("radius_miles"), 1, 10000, "Search radius"),
        "radius_latitude": _bounded_float(data.get("radius_latitude"), -90, 90, "Search latitude"),
        "radius_longitude": _bounded_float(data.get("radius_longitude"), -180, 180, "Search longitude"),
    }
    if job_filters is not None:
        criteria["job_filters"] = job_filters
    radius_values = (criteria["radius_miles"], criteria["radius_latitude"], criteria["radius_longitude"])
    if any(value is not None for value in radius_values) and any(value is None for value in radius_values):
        raise ValueError("Radius filtering requires miles, latitude, and longitude together")

    source_ids = _clean_strings(data.get("source_ids", []), limit=100, length=128)
    timestamp = now()
    with store.engine.begin() as connection:
        connection.execute(select(m.profiles.c.id).where(m.profiles.c.id == data["profile_id"])).one()
        if source_ids:
            existing_sources = set(connection.execute(
                select(m.job_sources.c.id).where(m.job_sources.c.id.in_(source_ids))).scalars())
            if existing_sources != set(source_ids):
                raise ValueError("Search profile references an unknown source")
        existing = connection.execute(select(m.search_profiles).where(
            m.search_profiles.c.id == data["id"])).mappings().one_or_none()
        if existing and existing["profile_id"] != data["profile_id"]:
            raise ValueError("Search profile belongs to a different career profile")
        created_at = existing["created_at"] if existing else timestamp
        changed = existing is not None and any((
            existing["profile_id"] != data["profile_id"],
            existing["name"] != data["name"].strip(),
            existing["enabled"] != data.get("enabled", True),
            existing["criteria"] != criteria,
            existing["source_ids"] != source_ids,
        ))
        upsert(connection, m.search_profiles, {
            "id": data["id"],
            "profile_id": data["profile_id"],
            "name": data["name"].strip(),
            "enabled": data.get("enabled", True),
            "criteria": criteria,
            "source_ids": source_ids,
            "created_at": created_at,
            "updated_at": timestamp,
        }, replace=True)
        store._audit(connection, data["id"], "SEARCH_PROFILE_SAVED", {
            "profile_id": data["profile_id"],
            "enabled": data.get("enabled", True),
            "source_count": len(source_ids),
        })
        if changed:
            for profile_id in sorted({existing["profile_id"], data["profile_id"]}):
                store._audit(connection, profile_id, "SETTING_CHANGED", {})
    return data["id"]


def list_search_profiles(store, profile_id=None):
    with store.engine.connect() as connection:
        statement = select(m.search_profiles)
        if profile_id:
            statement = statement.where(m.search_profiles.c.profile_id == profile_id)
        return [dict(row) for row in connection.execute(
            statement.order_by(m.search_profiles.c.name, m.search_profiles.c.id)).mappings()]


def get_search_profile(store, search_profile_id):
    with store.engine.connect() as connection:
        return dict(connection.execute(select(m.search_profiles).where(
            m.search_profiles.c.id == search_profile_id)).mappings().one())
