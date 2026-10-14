"""Validated, profile-scoped transfer for explicitly allowlisted settings."""
from uuid import uuid4

from sqlalchemy import delete, insert, select

from packages.persistence import models as m
from packages.persistence.store import now
from services.discovery.search_profiles import save_search_profile


SCHEMA_VERSION = 1
_SETTING_KEYS = {"search_profiles", "automation_rules", "source_configuration"}
_CRITERIA_KEYS = {
    "include_titles", "exclude_titles", "required_keywords", "optional_keywords",
    "excluded_keywords", "exact_phrases", "technology_aliases", "required_skills",
    "optional_skills", "excluded_skills", "company_include", "company_exclude", "remote",
    "work_arrangements", "countries", "states", "cities", "remote_regions", "employment_types",
    "experience_levels", "salary_min", "salary_max", "salary_period", "salary_known_only",
    "salary_unknown_allowed", "date_window", "date_start", "date_end", "clearance_levels",
    "exclude_clearance_required", "excluded_work_authorization_flags", "excluded_conditions",
    "max_travel_percent", "radius_miles", "radius_latitude", "radius_longitude", "job_filters",
}


def export_settings(store, profile_id):
    """Return a portable v1 document containing only settings owned by profile_id."""
    with store.engine.connect() as connection:
        exists = connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id)).scalar_one_or_none()
        if exists is None:
            raise ValueError("Career profile does not exist")
        rows = list(connection.execute(select(m.search_profiles).where(
            m.search_profiles.c.profile_id == profile_id).order_by(
                m.search_profiles.c.name, m.search_profiles.c.id)).mappings())
        sources = {row["id"] for row in connection.execute(
            select(m.job_sources.c.id).where(m.job_sources.c.id.in_(
                {source_id for row in rows for source_id in row["source_ids"] or []}))).mappings()}

    search_profiles = []
    source_configuration = []
    for row in rows:
        criteria = dict(row["criteria"] or {})
        scheduler = criteria.pop("scheduler", None)
        if set(criteria) - _CRITERIA_KEYS:
            raise ValueError("Stored saved search contains unsupported settings")
        search_profile = {"name": row["name"], "enabled": row["enabled"], "criteria": criteria}
        if scheduler is not None:
            search_profile["scheduler"] = scheduler
        search_profiles.append(search_profile)
        source_ids = list(row["source_ids"] or [])
        if set(source_ids) - sources:
            raise ValueError("Stored saved search references an unavailable source")
        source_configuration.append({"search_profile_name": row["name"], "source_ids": source_ids})

    return {"schema_version": SCHEMA_VERSION, "settings": {
        "search_profiles": search_profiles,
        # There is no persisted automation-rule model in this version.
        "automation_rules": [],
        "source_configuration": source_configuration,
    }}


def _validate_envelope(payload):
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "settings"}:
        raise ValueError("Settings document envelope is invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Settings document version is unsupported")
    settings = payload["settings"]
    if not isinstance(settings, dict) or set(settings) != _SETTING_KEYS:
        raise ValueError("Settings document categories are invalid")
    searches = settings["search_profiles"]
    automation = settings["automation_rules"]
    source_configuration = settings["source_configuration"]
    if not isinstance(searches, list) or len(searches) > 100:
        raise ValueError("Settings document has an invalid saved search list")
    if not isinstance(automation, list) or automation:
        raise ValueError("Automation rules are unsupported by this settings version")
    if not isinstance(source_configuration, list) or len(source_configuration) > 100:
        raise ValueError("Settings document has an invalid source configuration list")

    clean_searches = []
    names = set()
    for item in searches:
        if not isinstance(item, dict) or not {"name", "enabled", "criteria"} <= set(item) or set(item) - {
            "name", "enabled", "criteria", "scheduler",
        }:
            raise ValueError("Saved search settings have unsupported fields")
        name, enabled, criteria = item["name"], item["enabled"], item["criteria"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            raise ValueError("Saved search name is invalid")
        if name.strip().casefold() in names:
            raise ValueError("Saved search names must be unique")
        names.add(name.strip().casefold())
        if type(enabled) is not bool:
            raise ValueError("Saved search enabled flag must be boolean")
        if not isinstance(criteria, dict) or set(criteria) - _CRITERIA_KEYS:
            raise ValueError("Saved search criteria have unsupported fields")
        if "scheduler" in item and not isinstance(item["scheduler"], dict):
            raise ValueError("Saved search scheduler is invalid")
        clean_searches.append({
            "name": name.strip(), "enabled": enabled, "criteria": dict(criteria),
            **({"scheduler": item["scheduler"]} if "scheduler" in item else {}),
        })

    source_rows = {}
    for item in source_configuration:
        if not isinstance(item, dict) or set(item) != {"search_profile_name", "source_ids"}:
            raise ValueError("Source configuration has unsupported fields")
        name, source_ids = item["search_profile_name"], item["source_ids"]
        if not isinstance(name, str) or name.strip().casefold() not in names:
            raise ValueError("Source configuration references an unknown saved search")
        folded_name = name.strip().casefold()
        if folded_name in source_rows:
            raise ValueError("Source configuration contains a duplicate saved search")
        if not isinstance(source_ids, list) or len(source_ids) > 100 or any(
            not isinstance(value, str) or not value or len(value) > 128 for value in source_ids
        ) or len(set(source_ids)) != len(source_ids):
            raise ValueError("Source configuration source IDs are invalid")
        source_rows[folded_name] = list(source_ids)
    if set(source_rows) != names:
        raise ValueError("Each saved search must have source configuration")
    return clean_searches, source_rows


def _validate_searches_without_writes(store, profile_id, searches):
    """Reuse the existing saved-search validator against an isolated in-memory store."""
    from packages.persistence.store import Store

    validator = Store("sqlite:///:memory:")
    try:
        validator.migrate()
        with validator.engine.begin() as connection:
            connection.execute(insert(m.profiles).values(
                id=profile_id, name="Settings import validation", email="settings@example.invalid", version="1",
            ))
        validated = []
        for search in searches:
            data = {
                "id": uuid4().hex,
                "profile_id": profile_id,
                "name": search["name"],
                "enabled": search["enabled"],
                "source_ids": [],
                **search["criteria"],
            }
            if "scheduler" in search:
                data["scheduler"] = search["scheduler"]
            try:
                identifier = save_search_profile(validator, data)
            except TypeError:
                # JSON containers in scalar criteria can fail inside the
                # shared saved-search validator. Reject them before touching
                # the target store, without exposing imported values.
                raise ValueError("Saved search criteria are invalid") from None
            with validator.engine.connect() as connection:
                row = connection.execute(select(m.search_profiles).where(
                    m.search_profiles.c.id == identifier)).mappings().one()
            validated.append({"id": uuid4().hex, "name": row["name"], "enabled": row["enabled"],
                              "criteria": dict(row["criteria"]), "source_ids": []})
        return validated
    finally:
        validator.engine.dispose()


def import_settings(store, profile_id, payload):
    """Validate then atomically replace the target profile's allowlisted settings."""
    searches, source_ids_by_name = _validate_envelope(payload)
    clean_searches = _validate_searches_without_writes(store, profile_id, searches)
    all_source_ids = {source_id for values in source_ids_by_name.values() for source_id in values}
    with store.engine.connect() as connection:
        profile_exists = connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id)).scalar_one_or_none()
        if profile_exists is None:
            raise ValueError("Career profile does not exist")
        if all_source_ids:
            existing = set(connection.execute(select(m.job_sources.c.id).where(
                m.job_sources.c.id.in_(all_source_ids))).scalars())
            if existing != all_source_ids:
                raise ValueError("Source configuration references an unavailable source")

    timestamp = now()
    for row in clean_searches:
        row["source_ids"] = source_ids_by_name[row["name"].casefold()]
    with store.engine.begin() as connection:
        connection.execute(delete(m.search_profiles).where(m.search_profiles.c.profile_id == profile_id))
        for row in clean_searches:
            connection.execute(insert(m.search_profiles).values(
                id=row["id"], profile_id=profile_id, name=row["name"], enabled=row["enabled"],
                criteria=row["criteria"], source_ids=row["source_ids"], created_at=timestamp,
                updated_at=timestamp,
            ))
        store._audit(connection, profile_id, "SETTINGS_IMPORTED", {"saved_search_count": len(clean_searches)})
    return {"imported_count": len(clean_searches), "search_profile_count": len(clean_searches),
            "automation_rule_count": 0, "source_configuration_count": len(clean_searches)}


__all__ = ["SCHEMA_VERSION", "export_settings", "import_settings"]
