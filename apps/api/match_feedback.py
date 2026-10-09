"""Profile/job-scoped adapter for persisted, advisory match feedback."""
from __future__ import annotations

from importlib import import_module
from typing import Any, Mapping

from fastapi import APIRouter, Body, HTTPException


_CATEGORIES = frozenset({
    "GOOD_MATCH", "BAD_MATCH", "TOO_SENIOR", "WRONG_TECHNOLOGY",
    "WRONG_LOCATION", "BAD_SALARY", "BAD_COMPANY", "NOT_INTERESTED",
})
_MAX_REQUEST_ID = 128
_MAX_ID = 128
_MAX_TEXT = 2000


def _error(status_code: int, code: str) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code})


def _text(value: Any, *, max_length: int = _MAX_TEXT) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_length


def _category(value: Any) -> bool:
    return isinstance(value, str) and value in _CATEGORIES


def _service():
    """Load Hera's event-backed helper at call time for independently stackable slices."""
    try:
        module = import_module("services.matching.user_match_feedback")
        if not callable(getattr(module, "record_feedback", None)):
            raise AttributeError("record_feedback")
        if not callable(getattr(module, "list_feedback", None)):
            raise AttributeError("list_feedback")
        return module
    except (ImportError, AttributeError):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")


def _invoke(operation, conflict_type, *args):
    try:
        return operation(*args)
    except Exception as error:
        if conflict_type is not None and isinstance(error, conflict_type):
            _error(409, "MATCH_FEEDBACK_REQUEST_CONFLICT")
        if isinstance(error, LookupError):
            _error(404, "MATCH_FEEDBACK_SCOPE_NOT_FOUND")
        if isinstance(error, ValueError):
            _error(422, "MATCH_FEEDBACK_INVALID_REQUEST")
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")


def _confirmation(value: Any, profile_id: str, job_id: str) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or set(value) != {
                "profile_id", "job_id", "feedback", "replayed", "preference_proposal",
            }
            or value.get("profile_id") != profile_id
            or value.get("job_id") != job_id
            or type(value.get("replayed")) is not bool):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    feedback = value.get("feedback")
    if (not isinstance(feedback, Mapping)
            or set(feedback) != {"id", "category", "created_at"}
            or not _text(feedback.get("id"), max_length=_MAX_ID)
            or not _category(feedback.get("category"))
            or not _text(feedback.get("created_at"), max_length=64)):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    proposal = value.get("preference_proposal")
    if proposal is not None:
        if (not isinstance(proposal, Mapping)
                or not {"dimension", "direction", "explanation", "review_required"}.issubset(proposal)
                or not _text(proposal.get("dimension"), max_length=100)
                or not _text(proposal.get("direction"), max_length=100)
                or not _text(proposal.get("explanation"), max_length=_MAX_TEXT)
                or proposal.get("review_required") is not True):
            _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
        proposal = {
            "dimension": "overall" if proposal["dimension"] == "overall_fit" else proposal["dimension"],
            "direction": str(proposal["direction"]).lower(),
            "explanation": proposal["explanation"],
            "review_required": True,
        }
    return {
        "profile_id": profile_id,
        "job_id": job_id,
        "feedback": {key: feedback[key] for key in ("id", "category", "created_at")},
        "replayed": value["replayed"],
        "preference_proposal": proposal,
    }


def _history(value: Any, profile_id: str, job_id: str) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or set(value) != {"profile_id", "job_id", "items", "latest"}
            or value.get("profile_id") != profile_id
            or value.get("job_id") != job_id
            or not isinstance(value.get("items"), list)):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")

    def normalize_item(item: Any) -> dict[str, str]:
        if (not isinstance(item, Mapping)
                or set(item) != {"id", "category", "created_at"}
                or not _text(item.get("id"), max_length=_MAX_ID)
                or not _category(item.get("category"))
                or not _text(item.get("created_at"), max_length=64)):
            _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
        return {key: item[key] for key in ("id", "category", "created_at")}

    items = [normalize_item(item) for item in value["items"]]
    if len({item["id"] for item in items}) != len(items):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    latest = value.get("latest")
    latest = None if latest is None else normalize_item(latest)
    if (bool(items) != (latest is not None)
            or (latest is not None and latest not in items)):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    return {"profile_id": profile_id, "job_id": job_id, "items": items, "latest": latest}


def _normalize_applied_preference(value: Any, profile_id: str) -> dict[str, str]:
    fields = {"id", "job_id", "feedback_id", "category", "dimension", "direction", "created_at"}
    if (not isinstance(value, Mapping) or set(value) != fields
            or not all(_text(value.get(key), max_length=_MAX_ID)
                       for key in ("id", "job_id", "feedback_id"))
            or not _category(value.get("category"))
            or value.get("dimension") not in {
                "overall", "seniority", "technology", "location", "salary", "company",
            }
            or value.get("direction") not in {"INCREASE", "DECREASE"}
            or not _text(value.get("created_at"), max_length=64)):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    return {key: value[key] for key in fields}


def _preference_history(value: Any, profile_id: str) -> dict[str, Any]:
    if (not isinstance(value, Mapping)
            or set(value) != {"profile_id", "items", "by_dimension"}
            or value.get("profile_id") != profile_id
            or not isinstance(value.get("items"), list)
            or not isinstance(value.get("by_dimension"), Mapping)):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    items = [_normalize_applied_preference(item, profile_id) for item in value["items"]]
    if len({item["id"] for item in items}) != len(items):
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    latest_by_dimension = {}
    for item in items:
        latest_by_dimension[item["dimension"]] = item
    supplied = {
        dimension: _normalize_applied_preference(item, profile_id)
        for dimension, item in value["by_dimension"].items()
    }
    if supplied != latest_by_dimension:
        _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
    return {"profile_id": profile_id, "items": items, "by_dimension": latest_by_dimension}


def create_match_feedback_router(store) -> APIRouter:
    """Build feedback history and explicit, durable preference-review endpoints."""
    router = APIRouter()

    @router.post("/profiles/{profile_id}/jobs/{job_id}/match-feedback")
    def submit_match_feedback(profile_id: str, job_id: str, payload: object = Body(...)):
        if (not _text(profile_id, max_length=_MAX_ID)
                or not _text(job_id, max_length=_MAX_ID)
                or not isinstance(payload, dict)
                or set(payload) != {"category", "request_id"}
                or not _category(payload.get("category"))
                or not _text(payload.get("request_id"), max_length=_MAX_REQUEST_ID)
                or payload["request_id"] != payload["request_id"].strip()):
            _error(422, "MATCH_FEEDBACK_INVALID_REQUEST")

        service = _service()
        conflict_type = getattr(service, "FeedbackConflict", None)
        result = _invoke(
            service.record_feedback,
            conflict_type,
            store, profile_id, job_id, payload["category"], payload["request_id"],
        )
        return _confirmation(result, profile_id, job_id)

    @router.get("/profiles/{profile_id}/jobs/{job_id}/match-feedback")
    def read_match_feedback(profile_id: str, job_id: str):
        if (not _text(profile_id, max_length=_MAX_ID)
                or not _text(job_id, max_length=_MAX_ID)):
            _error(404, "MATCH_FEEDBACK_SCOPE_NOT_FOUND")
        service = _service()
        conflict_type = getattr(service, "FeedbackConflict", None)
        result = _invoke(service.list_feedback, conflict_type, store, profile_id, job_id)
        return _history(result, profile_id, job_id)

    @router.post("/profiles/{profile_id}/match-preferences")
    def apply_match_preference(profile_id: str, payload: object = Body(...)):
        if (not _text(profile_id, max_length=_MAX_ID)
                or not isinstance(payload, dict)
                or set(payload) != {"feedback_id", "dimension", "direction", "request_id"}
                or not _text(payload.get("feedback_id"), max_length=_MAX_ID)
                or payload.get("dimension") not in {
                    "overall", "overall_fit", "seniority", "technology", "location", "salary", "company",
                }
                or payload.get("direction") not in {"increase", "decrease"}
                or not _text(payload.get("request_id"), max_length=_MAX_REQUEST_ID)
                or payload["request_id"] != payload["request_id"].strip()):
            _error(422, "MATCH_FEEDBACK_PREFERENCE_INVALID_REQUEST")

        service = _service()
        conflict_type = getattr(service, "FeedbackConflict", None)
        result = _invoke(
            service.apply_feedback_preference,
            conflict_type,
            store, profile_id, payload["feedback_id"], payload["dimension"],
            payload["direction"], payload["request_id"],
        )
        if (not isinstance(result, Mapping)
                or set(result) != {"profile_id", "replayed", "preference"}
                or result.get("profile_id") != profile_id
                or type(result.get("replayed")) is not bool):
            _error(503, "MATCH_FEEDBACK_SERVICE_UNAVAILABLE")
        return {
            "profile_id": profile_id,
            "replayed": result["replayed"],
            "preference": _normalize_applied_preference(result["preference"], profile_id),
        }

    @router.get("/profiles/{profile_id}/match-preferences")
    def read_applied_match_preferences(profile_id: str):
        if not _text(profile_id, max_length=_MAX_ID):
            _error(404, "MATCH_FEEDBACK_SCOPE_NOT_FOUND")
        service = _service()
        conflict_type = getattr(service, "FeedbackConflict", None)
        result = _invoke(service.list_feedback_preferences, conflict_type, store, profile_id)
        return _preference_history(result, profile_id)

    return router
