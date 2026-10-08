"""Saved-search-scoped, versioned role-scoring profile API."""
from math import isfinite
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import now
from services.matching.scoring_profiles import resolve_role_scoring_profile


_PROFILE_INPUT_FIELDS = {"schema_version", "role_family", "weights", "thresholds"}
_PROFILE_FIELDS = _PROFILE_INPUT_FIELDS | {"id", "version"}
_PROFILE_CONFIG_KEY = "role_scoring_profile"


def _raise_api_error(status_code: int, code: str):
    raise HTTPException(status_code=status_code, detail={"code": code})


def _normalize_profile(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != _PROFILE_INPUT_FIELDS:
        raise ValueError("SCORING_PROFILE_INVALID")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("SCORING_PROFILE_INVALID")

    role_family = value.get("role_family")
    if (not isinstance(role_family, str) or not role_family.strip()
            or len(role_family.strip()) > 100):
        raise ValueError("SCORING_PROFILE_INVALID")

    normalized = {
        "schema_version": 1,
        "role_family": " ".join(role_family.split()),
        "weights": {},
        "thresholds": {},
    }
    for field in ("weights", "thresholds"):
        values = value.get(field)
        if not isinstance(values, dict):
            raise ValueError("SCORING_PROFILE_INVALID")
        for dimension, amount in values.items():
            if (not isinstance(dimension, str) or not dimension.strip()
                    or len(dimension.strip()) > 100
                    or isinstance(amount, bool) or not isinstance(amount, (int, float))
                    or not 0 <= amount <= 100 or not isfinite(amount)):
                raise ValueError("SCORING_PROFILE_INVALID")
            key = dimension.strip()
            if key in normalized[field]:
                raise ValueError("SCORING_PROFILE_INVALID")
            normalized[field][key] = float(amount)
    return normalized


def _normalize_stored_profile(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != _PROFILE_FIELDS:
        raise ValueError("SCORING_PROFILE_STORED_CONFIG_INVALID")
    profile_id = value.get("id")
    version = value.get("version")
    if (not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 200
            or not isinstance(version, str) or not version.strip() or len(version) > 200):
        raise ValueError("SCORING_PROFILE_STORED_CONFIG_INVALID")
    normalized = _normalize_profile({
        key: value[key] for key in _PROFILE_INPUT_FIELDS
    })
    return {"id": profile_id.strip(), "version": version.strip(), **normalized}


def _normalize_update(value: Any) -> tuple[str | None, dict]:
    if not isinstance(value, dict) or set(value) != {"expected_version", "profile"}:
        raise ValueError("SCORING_PROFILE_REQUEST_INVALID")
    expected_version = value["expected_version"]
    if (expected_version is not None
            and (not isinstance(expected_version, str) or not expected_version.strip()
                 or len(expected_version) > 200)):
        raise ValueError("SCORING_PROFILE_REQUEST_INVALID")
    try:
        profile = _normalize_profile(value["profile"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("SCORING_PROFILE_INVALID") from error
    return expected_version, profile


def _load_search(store, profile_id: str, search_profile_id: str) -> dict:
    try:
        store.profile(profile_id)
    except NoResultFound:
        _raise_api_error(404, "SCORING_PROFILE_PROFILE_NOT_FOUND")
    with store.engine.connect() as connection:
        search = connection.execute(select(m.search_profiles).where(
            m.search_profiles.c.id == search_profile_id,
        )).mappings().one_or_none()
    if search is None:
        _raise_api_error(404, "SCORING_PROFILE_SEARCH_NOT_FOUND")
    if search["profile_id"] != profile_id:
        _raise_api_error(409, "SCORING_PROFILE_PROFILE_MISMATCH")
    return dict(search)


def _stored_profile(criteria: Any) -> dict | None:
    if not isinstance(criteria, dict):
        _raise_api_error(409, "SCORING_PROFILE_SEARCH_CRITERIA_INVALID")
    value = criteria.get(_PROFILE_CONFIG_KEY)
    if value is None:
        return None
    try:
        return _normalize_stored_profile(value)
    except (KeyError, TypeError, ValueError):
        _raise_api_error(409, "SCORING_PROFILE_STORED_CONFIG_INVALID")


def create_scoring_profiles_router(store) -> APIRouter:
    """Expose optimistic profile CRUD and an optional family-resolution read."""
    router = APIRouter()

    @router.get(
        "/profiles/{profile_id}/search-profiles/{search_profile_id}/role-scoring-profile"
    )
    def read_scoring_profile(profile_id: str, search_profile_id: str,
                             role_family: str | None = None):
        search = _load_search(store, profile_id, search_profile_id)
        profile = _stored_profile(search.get("criteria"))
        if role_family is None:
            return {"profile": profile}

        profiles_by_role_family = (
            {profile["role_family"]: profile} if profile is not None else {}
        )
        try:
            resolution = resolve_role_scoring_profile(
                role_family, profiles_by_role_family,
            )
        except ValueError:
            _raise_api_error(422, "SCORING_PROFILE_ROLE_FAMILY_INVALID")
        return {
            "profile": resolution.profile,
            "resolution": resolution.explanation,
        }

    @router.put(
        "/profiles/{profile_id}/search-profiles/{search_profile_id}/role-scoring-profile"
    )
    def write_scoring_profile(profile_id: str, search_profile_id: str,
                              value: Any = Body(...)):
        try:
            expected_version, normalized = _normalize_update(value)
        except (KeyError, TypeError, ValueError) as error:
            code = str(error) or "SCORING_PROFILE_REQUEST_INVALID"
            if code not in {"SCORING_PROFILE_INVALID", "SCORING_PROFILE_REQUEST_INVALID"}:
                code = "SCORING_PROFILE_REQUEST_INVALID"
            _raise_api_error(422, code)

        try:
            store.profile(profile_id)
        except NoResultFound:
            _raise_api_error(404, "SCORING_PROFILE_PROFILE_NOT_FOUND")

        with store.engine.begin() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            query = select(m.search_profiles).where(
                m.search_profiles.c.id == search_profile_id,
            )
            if connection.dialect.name != "sqlite":
                query = query.with_for_update()
            search = connection.execute(query).mappings().one_or_none()
            if search is None:
                _raise_api_error(404, "SCORING_PROFILE_SEARCH_NOT_FOUND")
            if search["profile_id"] != profile_id:
                _raise_api_error(409, "SCORING_PROFILE_PROFILE_MISMATCH")
            criteria = search["criteria"]
            if not isinstance(criteria, dict):
                _raise_api_error(409, "SCORING_PROFILE_SEARCH_CRITERIA_INVALID")

            current_value = criteria.get(_PROFILE_CONFIG_KEY)
            if current_value is None:
                current = None
            else:
                try:
                    current = _normalize_stored_profile(current_value)
                except (KeyError, TypeError, ValueError):
                    _raise_api_error(409, "SCORING_PROFILE_STORED_CONFIG_INVALID")

            if ((current is None and expected_version is not None)
                    or (current is not None and expected_version != current["version"])):
                _raise_api_error(409, "SCORING_PROFILE_VERSION_CONFLICT")

            saved = {
                "id": current["id"] if current is not None else uuid4().hex,
                "version": uuid4().hex,
                **normalized,
            }
            updated_criteria = dict(criteria)
            updated_criteria[_PROFILE_CONFIG_KEY] = saved
            connection.execute(update(m.search_profiles).where(
                m.search_profiles.c.id == search_profile_id,
            ).values(criteria=updated_criteria, updated_at=now()))
            store._audit(connection, profile_id, "SETTING_CHANGED", {
                "setting": _PROFILE_CONFIG_KEY,
                "search_profile_id": search_profile_id,
            })

        return {"profile": saved}

    return router
