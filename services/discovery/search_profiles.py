"""Persistent data-driven search profiles for one local MJobs workspace."""
from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now, upsert


EMPLOYMENT_TYPES = {
    "FULL_TIME", "PART_TIME", "CONTRACT", "CONTRACT_TO_HIRE",
    "TEMPORARY", "INTERNSHIP", "OTHER",
}
REMOTE_PREFERENCES = {"ANY", "REQUIRED", "EXCLUDED"}


def _clean_strings(values, *, limit=100, length=200):
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError("Search profile list is invalid or too large")
    cleaned = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > length:
            raise ValueError("Search profile list contains an invalid value")
        item = value.strip()
        if item.casefold() not in {existing.casefold() for existing in cleaned}:
            cleaned.append(item)
    return cleaned


def save_search_profile(store, data):
    for key in ("id", "profile_id", "name"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError("Search profile identity, owner, and name are required")
    if len(data["id"]) > 128 or len(data["profile_id"]) > 128 or len(data["name"].strip()) > 200:
        raise ValueError("Search profile identity or name is too long")
    if type(data.get("enabled", True)) is not bool:
        raise ValueError("Search profile enabled must be boolean")
    remote = data.get("remote", "ANY")
    if remote not in REMOTE_PREFERENCES:
        raise ValueError("Search profile remote preference is invalid")
    employment = _clean_strings(data.get("employment_types", []), limit=20, length=32)
    if any(value not in EMPLOYMENT_TYPES for value in employment):
        raise ValueError("Search profile employment type is invalid")
    titles = _clean_strings(data.get("include_titles", []), limit=100, length=200)
    source_ids = _clean_strings(data.get("source_ids", []), limit=100, length=128)
    criteria = {"include_titles": titles, "remote": remote, "employment_types": employment}
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
        created_at = existing["created_at"] if existing else timestamp
        upsert(connection, m.search_profiles, {
            "id": data["id"], "profile_id": data["profile_id"], "name": data["name"].strip(),
            "enabled": data.get("enabled", True), "criteria": criteria, "source_ids": source_ids,
            "created_at": created_at, "updated_at": timestamp,
        }, replace=True)
        store._audit(connection, data["id"], "SEARCH_PROFILE_SAVED", {
            "profile_id": data["profile_id"], "enabled": data.get("enabled", True),
            "source_count": len(source_ids),
        })
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
