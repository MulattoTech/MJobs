"""Validated, profile-scoped transfer for explicitly allowlisted settings."""
import re
from uuid import uuid4

from sqlalchemy import delete, insert, select

from packages.persistence import models as m
from packages.persistence.store import now
from services.discovery.search_profiles import _clean_search_scheduler, save_search_profile
from services.discovery.sources.collect import configure_source


SCHEMA_VERSION = 1
_SETTING_KEYS = {"search_profiles", "automation_rules", "source_configuration"}
_SOURCE_POLICY_KEYS = {
    "id", "provider", "tenant", "enabled", "poll_interval", "concurrency",
    "request_timeout", "rate_limit", "jitter_seconds",
}
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
        sources = {row["id"]: dict(row) for row in connection.execute(
            select(m.job_sources).where(m.job_sources.c.id.in_(
                {source_id for row in rows for source_id in row["source_ids"] or []}))).mappings()}

    search_profiles = []
    source_configuration = []
    automation_rules = []
    portable_sources = {policy["id"]: policy for policy in _validate_source_policies([
        {key: source[key] for key in _SOURCE_POLICY_KEYS} for source in sources.values()])}
    for row in rows:
        criteria = dict(row["criteria"] or {})
        scheduler = criteria.pop("scheduler", None)
        if set(criteria) - _CRITERIA_KEYS:
            raise ValueError("Stored saved search contains unsupported settings")
        search_profile = {"name": row["name"], "enabled": row["enabled"], "criteria": criteria}
        if scheduler is not None:
            scheduler = _clean_search_scheduler(scheduler)
            search_profile["scheduler"] = scheduler
            automation_rules.append({"kind": "scheduled_search",
                                     "search_profile_name": row["name"], **scheduler})
        search_profiles.append(search_profile)
        source_ids = list(row["source_ids"] or [])
        if set(source_ids) - sources.keys():
            raise ValueError("Stored saved search references an unavailable source")
        policies = [portable_sources[source_id] for source_id in source_ids]
        source_configuration.append({"search_profile_name": row["name"], "source_ids": source_ids,
                                     "sources": policies})

    return {"schema_version": SCHEMA_VERSION, "settings": {
        "search_profiles": search_profiles,
        "automation_rules": automation_rules,
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
    if not isinstance(automation, list) or len(automation) > 100:
        raise ValueError("Settings document has an invalid automation rule list")
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

    by_name = {search["name"].casefold(): search for search in clean_searches}
    rule_names = set()
    for rule in automation:
        if not isinstance(rule, dict) or set(rule) != {
                "kind", "search_profile_name", "enabled", "interval_seconds"}:
            raise ValueError("Automation rule has unsupported fields")
        name = rule["search_profile_name"]
        if (rule["kind"] != "scheduled_search" or not isinstance(name, str)
                or name.strip().casefold() not in names):
            raise ValueError("Automation rule references an unsupported action or saved search")
        folded = name.strip().casefold()
        if folded in rule_names:
            raise ValueError("Automation rules contain a duplicate saved search")
        rule_names.add(folded)
        scheduler = _clean_search_scheduler({key: rule[key] for key in ("enabled", "interval_seconds")})
        search = by_name[folded]
        if "scheduler" in search and _clean_search_scheduler(search["scheduler"]) != scheduler:
            raise ValueError("Automation rule conflicts with saved search scheduler")
        search["scheduler"] = scheduler

    source_rows = {}
    source_policies = {}
    for item in source_configuration:
        if (not isinstance(item, dict) or not {"search_profile_name", "source_ids"} <= set(item)
                or set(item) - {"search_profile_name", "source_ids", "sources"}):
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
        if "sources" in item:
            policies = item["sources"]
            if (not isinstance(policies, list) or len(policies) > 100
                    or any(not isinstance(policy, dict) or set(policy) != _SOURCE_POLICY_KEYS
                           or not isinstance(policy["id"], str) for policy in policies)):
                raise ValueError("Source policy list is invalid")
            policy_ids = [policy["id"] for policy in policies]
            if len(set(policy_ids)) != len(policy_ids) or set(policy_ids) != set(source_ids):
                raise ValueError("Source policies do not match saved search source IDs")
            for policy in policies:
                identifier = policy["id"]
                if identifier in source_policies and source_policies[identifier] != policy:
                    raise ValueError("Source configuration contains conflicting source policies")
                source_policies[identifier] = policy
            if len(source_policies) > 100:
                raise ValueError("Settings document has too many source policies")
    if set(source_rows) != names:
        raise ValueError("Each saved search must have source configuration")
    _validate_source_policies(list(source_policies.values()))
    return clean_searches, source_rows, source_policies


def _validate_source_policies(policies):
    """Reuse source admission in an isolated store, never fetching providers."""
    from packages.persistence.store import Store

    if not isinstance(policies, list) or len(policies) > 100:
        raise ValueError("Source policy list is invalid")
    if not policies:
        return []
    validator = Store("sqlite:///:memory:")
    try:
        validator.migrate()
        seen = set()
        clean = []
        for policy in policies:
            if (not isinstance(policy, dict) or set(policy) != _SOURCE_POLICY_KEYS
                    or not isinstance(policy["provider"], str)
                    or not isinstance(policy["tenant"], str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:-]{0,127}", policy["tenant"])):
                raise ValueError("Source policy fields are invalid")
            try:
                identifier = configure_source(validator, **{key: policy[key] for key in
                                                            _SOURCE_POLICY_KEYS - {"id"}})
            except (TypeError, ValueError):
                raise ValueError("Source policy is invalid") from None
            if identifier != policy["id"] or identifier in seen:
                raise ValueError("Source policy identity is invalid or duplicated")
            seen.add(identifier)
            with validator.engine.connect() as connection:
                row = connection.execute(select(m.job_sources).where(
                    m.job_sources.c.id == identifier)).mappings().one()
                canonical = {key: row[key] for key in _SOURCE_POLICY_KEYS}
            if canonical != policy:
                raise ValueError("Source policy identity must be canonical")
            clean.append(canonical)
        return clean
    finally:
        validator.engine.dispose()


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
    searches, source_ids_by_name, source_policies = _validate_envelope(payload)
    clean_searches = _validate_searches_without_writes(store, profile_id, searches)
    all_source_ids = {source_id for values in source_ids_by_name.values() for source_id in values}
    timestamp = now()
    for row in clean_searches:
        row["source_ids"] = source_ids_by_name[row["name"].casefold()]
    with store.engine.begin() as connection:
        # Serialize profile replacement and source-policy admission across local
        # SQLite connections. PostgreSQL source rows are locked below.
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        profile_exists = connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id).with_for_update()).scalar_one_or_none()
        if profile_exists is None:
            raise ValueError("Career profile does not exist")
        for identifier, policy in sorted(source_policies.items()):
            configure_source(store, **{key: policy[key] for key in _SOURCE_POLICY_KEYS - {"id"}},
                             connection=connection, preserve_existing=True)
            existing = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == identifier).with_for_update()).mappings().one()
            if any(existing[key] != policy[key] for key in _SOURCE_POLICY_KEYS):
                raise ValueError("Existing shared source policy differs; review Sources before import")
        if all_source_ids:
            existing = set(connection.execute(select(m.job_sources.c.id).where(
                m.job_sources.c.id.in_(all_source_ids))).scalars())
            if existing != all_source_ids:
                raise ValueError("Source configuration references an unavailable source")

        connection.execute(delete(m.search_profiles).where(m.search_profiles.c.profile_id == profile_id))
        for row in clean_searches:
            connection.execute(insert(m.search_profiles).values(
                id=row["id"], profile_id=profile_id, name=row["name"], enabled=row["enabled"],
                criteria=row["criteria"], source_ids=row["source_ids"], created_at=timestamp,
                updated_at=timestamp,
            ))
        store._audit(connection, profile_id, "SETTINGS_IMPORTED", {"saved_search_count": len(clean_searches)})
    rule_count = sum("scheduler" in row["criteria"] for row in clean_searches)
    return {"imported_count": len(clean_searches), "search_profile_count": len(clean_searches),
            "automation_rule_count": rule_count, "source_configuration_count": len(clean_searches)}


__all__ = ["SCHEMA_VERSION", "export_settings", "import_settings"]
