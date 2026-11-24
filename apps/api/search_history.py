"""Profile-scoped, read-only API for durable search-run history."""
from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import NoResultFound
from services.discovery.search_history import (
    MAX_SEARCH_HISTORY_CURSOR_LENGTH,
    is_search_history_id,
    normalize_posting_activity,
)


_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


def _error(status_code: int, code: str):
    raise HTTPException(status_code=status_code, detail={"code": code})


def _service_lister():
    # Keep the API composable while the history persistence service is developed
    # independently; root mounts the router once the service dependency is present.
    from services.discovery.search_history import list_search_history

    return list_search_history


def _project_run(run, profile_id: str):
    if not isinstance(run, Mapping):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    if run.get("profile_id") != profile_id:
        _error(500, "SEARCH_HISTORY_SCOPE_MISMATCH")
    search_profile_id = run.get("search_profile_id")
    if search_profile_id is not None and not is_search_history_id(search_profile_id):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    if not isinstance(run.get("run_id"), str) or not run["run_id"].strip():
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    if (not isinstance(run.get("source"), str) or not run["source"].strip()
            or not isinstance(run.get("query"), str)):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    if not isinstance(run.get("filters"), Mapping):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    if not isinstance(run.get("started_at"), str) or not isinstance(run.get("finished_at"), str):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    if run.get("status") not in ("COMPLETED", "FAILED", "CANCELLED"):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    counts = (run.get("results"), run.get("new_jobs"), run.get("duplicates"))
    if any(value is not None and (type(value) is not int or value < 0)
           for value in counts):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    errors = run.get("errors")
    if not isinstance(errors, list) or any(not isinstance(value, str) for value in errors):
        _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
    item = {
        "id": run["run_id"],
        "profile_id": profile_id,
        "source": run["source"],
        "query": run["query"],
        "filters": dict(run["filters"]),
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "status": run["status"],
        "results": run["results"],
        "new_jobs": run["new_jobs"],
        "duplicates": run["duplicates"],
        "errors": list(errors),
    }
    if "search_profile_id" in run:
        item["search_profile_id"] = search_profile_id
    if "posting_activity" in run:
        try:
            activity = normalize_posting_activity(run["posting_activity"], item["results"])
        except ValueError:
            _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
        if item["status"] != "COMPLETED" or item["duplicates"] != activity["unchanged"]:
            _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
        item["posting_activity"] = activity
    return item


def create_search_history_router(store, *, history_lister=None) -> APIRouter:
    """Build a router that lists only the requested profile's persisted runs."""
    router = APIRouter()
    list_history = history_lister or _service_lister()

    @router.get("/profiles/{profile_id}/search-history")
    def read_search_history(
        profile_id: str,
        limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
        cursor: str | None = Query(default=None, max_length=MAX_SEARCH_HISTORY_CURSOR_LENGTH),
        search_profile_id: str | None = Query(default=None, min_length=1, max_length=128),
    ):
        if search_profile_id is not None and not is_search_history_id(search_profile_id):
            _error(422, "SEARCH_HISTORY_SEARCH_PROFILE_ID_INVALID")
        try:
            store.profile(profile_id)
        except NoResultFound:
            _error(404, "SEARCH_HISTORY_PROFILE_NOT_FOUND")
        try:
            arguments = {"limit": limit, "cursor": cursor}
            if search_profile_id is not None:
                arguments["search_profile_id"] = search_profile_id
            result = list_history(store, profile_id, **arguments)
        except NoResultFound:
            _error(404, "SEARCH_HISTORY_PROFILE_NOT_FOUND")
        except ValueError as exc:
            if str(exc) == "SEARCH_HISTORY_CURSOR_INVALID":
                _error(422, "SEARCH_HISTORY_CURSOR_INVALID")
            if str(exc) == "SEARCH_HISTORY_LIMIT_INVALID":
                _error(422, "SEARCH_HISTORY_LIMIT_INVALID")
            _error(422, "SEARCH_HISTORY_REQUEST_INVALID")
        if (not isinstance(result, Mapping)
                or result.get("profile_id") != profile_id
                or not isinstance(result.get("runs"), list)
                or len(result["runs"]) > limit):
            _error(500, "SEARCH_HISTORY_SCOPE_MISMATCH"
                   if isinstance(result, Mapping) and result.get("profile_id") != profile_id
                   else "SEARCH_HISTORY_RESPONSE_INVALID")
        next_cursor = result.get("next_cursor")
        if (next_cursor is not None and
                (not isinstance(next_cursor, str) or not next_cursor
                 or len(next_cursor) > MAX_SEARCH_HISTORY_CURSOR_LENGTH)):
            _error(500, "SEARCH_HISTORY_RESPONSE_INVALID")
        items = [_project_run(run, profile_id) for run in result["runs"]]
        if search_profile_id is not None and any(
                item.get("search_profile_id") != search_profile_id for item in items):
            _error(500, "SEARCH_HISTORY_SCOPE_MISMATCH")
        return {
            "profile_id": profile_id,
            "items": items,
            "next_cursor": next_cursor,
        }

    return router


__all__ = ["create_search_history_router"]
