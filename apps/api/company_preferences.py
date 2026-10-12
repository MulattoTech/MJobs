"""Profile-scoped HTTP interface for explicit company preference decisions."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Body, HTTPException


_STATUSES = frozenset({"BLACKLISTED", "PREFERRED", "NEUTRAL"})
_MAX_ID = 128


def _error(status_code: int, code: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code})


def _text(value, *, optional=False) -> bool:
    if optional and value is None:
        return True
    return isinstance(value, str) and bool(value.strip()) and len(value) <= _MAX_ID


def _invoke(operation, *args):
    try:
        return operation(*args)
    except LookupError:
        _error(404, "COMPANY_PREFERENCE_SCOPE_NOT_FOUND")
    except ValueError:
        _error(422, "COMPANY_PREFERENCE_INVALID_REQUEST")
    except Exception:
        _error(503, "COMPANY_PREFERENCE_SERVICE_UNAVAILABLE")


def _preference(value, profile_id: str, company_id: str, *, write=False) -> dict:
    expected = {"profile_id", "company_id", "status", "updated_at"}
    if write:
        expected.add("replayed")
    if isinstance(value, Mapping) and 'discovery' in value:
        expected.add('discovery')
        discovery = value['discovery']
        if (not isinstance(discovery, Mapping) or set(discovery) != {'request_id', 'status', 'reason'}
                or not _text(discovery.get('request_id'), optional=True)
                or not _text(discovery.get('reason'), optional=True)
                or discovery.get('status') not in {'NOT_QUEUED', 'NEEDS_ORIGIN_REVIEW', 'QUEUED',
                    'RUNNING', 'COMPLETED', 'FAILED', 'DEAD_LETTERED', 'DISCARDED', 'BLOCKED', 'CANCELLED', 'DISMISSED'}):
            _error(503, "COMPANY_PREFERENCE_SERVICE_UNAVAILABLE")
    if (not isinstance(value, Mapping) or set(value) != expected
            or value.get("profile_id") != profile_id
            or value.get("company_id") != company_id
            or value.get("status") not in _STATUSES
            or not _text(value.get("updated_at"), optional=True)
            or (write and type(value.get("replayed")) is not bool)):
        _error(503, "COMPANY_PREFERENCE_SERVICE_UNAVAILABLE")
    normalized = {key: value[key] for key in ("profile_id", "company_id", "status", "updated_at")}
    if write:
        normalized["replayed"] = value["replayed"]
    if 'discovery' in value:
        normalized['discovery'] = dict(value['discovery'])
    return normalized


def create_company_preferences_router(store) -> APIRouter:
    router = APIRouter()

    @router.get("/profiles/{profile_id}/company-preferences")
    def list_preferences(profile_id: str):
        if not _text(profile_id):
            _error(404, "COMPANY_PREFERENCE_SCOPE_NOT_FOUND")
        from services.matching.company_preferences import list_company_preferences
        values = _invoke(list_company_preferences, store, profile_id)
        if not isinstance(values, list):
            _error(503, "COMPANY_PREFERENCE_SERVICE_UNAVAILABLE")
        preferences = []
        for value in values:
            if (not isinstance(value, Mapping)
                    or set(value) != {"profile_id", "company_id", "company", "status", "updated_at"}
                    or value.get("profile_id") != profile_id
                    or not _text(value.get("company_id"))
                    or not isinstance(value.get("company"), str)
                    or value.get("status") not in _STATUSES
                    or not _text(value.get("updated_at"))):
                _error(503, "COMPANY_PREFERENCE_SERVICE_UNAVAILABLE")
            preferences.append({key: value[key] for key in (
                "profile_id", "company_id", "company", "status", "updated_at")})
        return {"profile_id": profile_id, "preferences": preferences}

    @router.get("/profiles/{profile_id}/company-preferences/{company_id}")
    def read_preference(profile_id: str, company_id: str):
        if not _text(profile_id) or not _text(company_id):
            _error(404, "COMPANY_PREFERENCE_SCOPE_NOT_FOUND")
        from services.matching.company_preferences import get_company_preference
        return _preference(_invoke(get_company_preference, store, profile_id, company_id),
                           profile_id, company_id)

    @router.put("/profiles/{profile_id}/company-preferences/{company_id}")
    def write_preference(profile_id: str, company_id: str, payload: object = Body(...)):
        if (not _text(profile_id) or not _text(company_id)
                or not isinstance(payload, dict)
                or set(payload) not in ({"status", "request_id"}, {"status", "request_id", "job_id"})
                or ('job_id' in payload and (not _text(payload['job_id'])
                    or payload['job_id'] != payload['job_id'].strip()))
                or payload.get("status") not in _STATUSES
                or not _text(payload.get("request_id"))
                or payload["request_id"] != payload["request_id"].strip()):
            _error(422, "COMPANY_PREFERENCE_INVALID_REQUEST")
        from services.matching.company_preferences import (
            CompanyPreferenceConflict, set_company_preference,
        )
        try:
            value = set_company_preference(
                store, profile_id, company_id, payload["status"], payload["request_id"],
                **({'job_id': payload['job_id']} if 'job_id' in payload else {}))
        except CompanyPreferenceConflict:
            _error(409, "COMPANY_PREFERENCE_REQUEST_CONFLICT")
        except LookupError:
            _error(404, "COMPANY_PREFERENCE_SCOPE_NOT_FOUND")
        except ValueError:
            _error(422, "COMPANY_PREFERENCE_INVALID_REQUEST")
        except Exception:
            _error(503, "COMPANY_PREFERENCE_SERVICE_UNAVAILABLE")
        return _preference(value, profile_id, company_id, write=True)

    return router
