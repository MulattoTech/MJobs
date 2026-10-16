"""Durable, profile-scoped state transitions for local application pages."""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.applications.adapters.workday_navigation import (
    WORKDAY_STEP_ORDER,
    classify_step,
    decide_transition,
)


MAX_PAGES = 100
MAX_PROGRESS_BYTES = 256 * 1024
_PAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_KEY = re.compile(r"password|passcode|credential|cookie|session|token|secret", re.I)
_ACTIONS = {"SAVE_PROGRESS", "NEXT", "PREVIOUS", "REVIEW"}
_ENTITY_KIND = "multipage-application-state"
_EVENT_ACTION = "MULTIPAGE_STATE_SNAPSHOT"
_WORKDAY_PAGE_ORDER = tuple(step for step in WORKDAY_STEP_ORDER if step != "review")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class MultipageStateError(ValueError):
    """A request cannot be applied to the current application-page state."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MultipageStateConflict(MultipageStateError):
    """The caller's version or idempotency key conflicts with durable state."""

    def __init__(self, code: str = "MULTIPAGE_VERSION_CONFLICT"):
        super().__init__(code, "Application page state changed; reload it before continuing")


def _entity_id(profile_id: str, application_id: str) -> str:
    return identity(_ENTITY_KIND, profile_id, application_id)


def _event_id(profile_id: str, application_id: str, request_id: str) -> str:
    return identity(_ENTITY_KIND, profile_id, application_id, request_id)


def _page_id(value, field="page_id", *, allow_review=False):
    if (not isinstance(value, str) or not _PAGE_ID.fullmatch(value)
            or (value == "review" and not allow_review)):
        raise MultipageStateError("INVALID_PAGE_ID", f"{field} must be a stable application page identifier")
    return value


def _safe_progress(progress, page_order):
    if not isinstance(progress, dict):
        raise MultipageStateError("INVALID_PROGRESS", "progress must be an object keyed by page identifier")
    try:
        encoded = json.dumps(progress, ensure_ascii=False, allow_nan=False,
                              sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise MultipageStateError("INVALID_PROGRESS", "progress must contain JSON values") from None
    if len(encoded.encode("utf-8")) > MAX_PROGRESS_BYTES:
        raise MultipageStateError("INVALID_PROGRESS", "progress exceeds the supported size")
    normalized = json.loads(encoded)
    if any(not isinstance(page, str) or page not in page_order or not isinstance(answers, dict)
           for page, answers in normalized.items()):
        raise MultipageStateError("INVALID_PROGRESS", "progress must contain only known page objects")

    def reject_secret_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or _SECRET_KEY.search(key):
                    raise MultipageStateError("INVALID_PROGRESS", "credential fields cannot be stored as page progress")
                reject_secret_keys(item)
        elif isinstance(value, list):
            for item in value:
                reject_secret_keys(item)

    reject_secret_keys(normalized)
    return normalized


def _normalize_workday_navigation(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
            "expected_fingerprint", "current_fingerprint", "visited_fingerprints"}:
        raise MultipageStateError("INVALID_WORKDAY_STATE", "Workday navigation evidence is invalid")
    expected = value["expected_fingerprint"]
    current = value["current_fingerprint"]
    visited = value["visited_fingerprints"]
    if (expected is not None and (not isinstance(expected, str) or not _FINGERPRINT.fullmatch(expected))
            or not isinstance(current, str) or not _FINGERPRINT.fullmatch(current)
            or not isinstance(visited, list) or not 1 <= len(visited) <= MAX_PAGES
            or any(not isinstance(item, str) or not _FINGERPRINT.fullmatch(item) for item in visited)
            or len(set(visited)) != len(visited) or visited[-1] != current):
        raise MultipageStateError("INVALID_WORKDAY_STATE", "Workday navigation evidence is invalid")
    return {
        "expected_fingerprint": expected,
        "current_fingerprint": current,
        "visited_fingerprints": list(visited),
    }


def _normalize_request(payload):
    if not isinstance(payload, dict):
        raise MultipageStateError("INVALID_REQUEST", "Application page action must be an object")
    allowed = {"request_id", "action", "expected_version", "page_id", "page_order",
               "next_page_id", "previous_page_id", "progress", "workday_navigation"}
    if set(payload) - allowed:
        raise MultipageStateError("INVALID_REQUEST", "Application page action contains unsupported fields")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise MultipageStateError("INVALID_REQUEST_ID", "request_id must be a stable operation identifier")
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise MultipageStateError("INVALID_ACTION", "Application page action is not supported")
    version = payload.get("expected_version")
    if type(version) is not int or version < 0:
        raise MultipageStateError("INVALID_VERSION", "expected_version must be a non-negative integer")
    page = _page_id(payload.get("page_id"), allow_review=payload.get("action") == "PREVIOUS")
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        raise MultipageStateError("INVALID_PROGRESS", "progress must be an object keyed by page identifier")

    page_order = payload.get("page_order")
    if page_order is not None:
        if (not isinstance(page_order, list) or not 1 <= len(page_order) <= MAX_PAGES
                or any(not isinstance(item, str) or not _PAGE_ID.fullmatch(item) or item == "review"
                       for item in page_order)
                or len(set(page_order)) != len(page_order)):
            raise MultipageStateError("INVALID_PAGE_ORDER", "page_order must contain unique stable page identifiers")

    next_page_id = payload.get("next_page_id")
    previous_page_id = payload.get("previous_page_id")
    if next_page_id is not None:
        next_page_id = _page_id(next_page_id, "next_page_id")
    if previous_page_id is not None:
        previous_page_id = _page_id(previous_page_id, "previous_page_id")
    if ((action == "NEXT" and (next_page_id is None or previous_page_id is not None))
            or (action == "PREVIOUS" and (previous_page_id is None or next_page_id is not None))
            or (action in {"SAVE_PROGRESS", "REVIEW"}
                and (next_page_id is not None or previous_page_id is not None))):
        raise MultipageStateError("INVALID_REQUEST", "Navigation fields do not match the requested action")
    return {
        "request_id": request_id,
        "action": action,
        "expected_version": version,
        "page_id": page,
        "page_order": page_order,
        "next_page_id": next_page_id,
        "previous_page_id": previous_page_id,
        "progress": progress,
        "workday_navigation": _normalize_workday_navigation(payload.get("workday_navigation")),
    }


def _lock_and_load(connection, profile_id, application_id):
    profile = connection.execute(select(m.profiles.c.id).where(
        m.profiles.c.id == profile_id).with_for_update()).scalar_one_or_none()
    if profile is None:
        raise MultipageStateError("APPLICATION_NOT_FOUND", "Application was not found for this profile")
    application = connection.execute(select(m.applications).where(
        m.applications.c.id == application_id).with_for_update()).mappings().one_or_none()
    if application is None or application["profile_id"] != profile_id:
        raise MultipageStateError("APPLICATION_NOT_FOUND", "Application was not found for this profile")
    return application


def _latest_snapshot(connection, entity_id):
    rows = connection.execute(select(m.audit).where(
        m.audit.c.entity_id == entity_id,
        m.audit.c.action == _EVENT_ACTION,
    )).mappings().all()
    if not rows:
        return None
    return max(rows, key=lambda row: row["payload"]["state"]["version"])


def _response(profile_id, application_id, snapshot):
    if snapshot is None:
        return {
            "profile_id": profile_id,
            "application_id": application_id,
            "page_order": [],
            "current_page_id": None,
            "status": "IN_PROGRESS",
            "progress": {},
            "version": 0,
            "updated_at": None,
            "manual_intervention_required": True,
        }
    state = snapshot["payload"]["state"]
    response = {
        "profile_id": profile_id,
        "application_id": application_id,
        "page_order": list(state["page_order"]),
        "current_page_id": state["current_page_id"],
        "status": state["status"],
        "progress": state["progress"],
        "version": state["version"],
        "updated_at": state["updated_at"],
        "manual_intervention_required": True,
    }
    if "workday_navigation" in state:
        response["workday_navigation"] = state["workday_navigation"]
    return response


def load_multipage_state(store, profile_id: str, application_id: str) -> dict:
    """Read the durable page snapshot for an application owned by this profile."""
    entity_id = _entity_id(profile_id, application_id)
    with store.engine.connect() as connection:
        application = connection.execute(select(m.applications.c.profile_id).where(
            m.applications.c.id == application_id)).scalar_one_or_none()
        profile = connection.execute(select(m.profiles.c.id).where(
            m.profiles.c.id == profile_id)).scalar_one_or_none()
        if application is None or application != profile_id or profile is None:
            raise MultipageStateError("APPLICATION_NOT_FOUND", "Application was not found for this profile")
        snapshot = _latest_snapshot(connection, entity_id)
        return _response(profile_id, application_id, snapshot)


def _validate_transition(current, request):
    action = request["action"]
    page = request["page_id"]
    version = request["expected_version"]
    page_order = request["page_order"]
    workday_navigation = request["workday_navigation"]
    if current is None:
        if action != "SAVE_PROGRESS" or version != 0 or page_order is None:
            raise MultipageStateError("INVALID_TRANSITION", "The first page must be saved before navigation")
        if page != page_order[0]:
            raise MultipageStateError("INVALID_TRANSITION", "The initial page must be first in page_order")
        order = page_order
        status = "IN_PROGRESS"
        current_page = page
        if workday_navigation is not None and (
                workday_navigation["expected_fingerprint"] is not None
                or order != list(_WORKDAY_PAGE_ORDER)
                or page != _WORKDAY_PAGE_ORDER[0]
                or workday_navigation["visited_fingerprints"] != [workday_navigation["current_fingerprint"]]):
            raise MultipageStateError("INVALID_WORKDAY_STATE", "Workday navigation must start at its first observed page")
    else:
        state = current["payload"]["state"]
        if version != state["version"]:
            raise MultipageStateConflict()
        if page_order is not None and page_order != state["page_order"]:
            raise MultipageStateError("INVALID_PAGE_ORDER", "page_order cannot change after initialization")
        order = state["page_order"]
        status = state["status"]
        current_page = state["current_page_id"]
        stored_workday_navigation = state.get("workday_navigation")
        if stored_workday_navigation is not None:
            if workday_navigation is None:
                raise MultipageStateError("WORKDAY_OBSERVATION_REQUIRED", "Workday navigation requires confirmed observation evidence")
            if action not in {"NEXT", "PREVIOUS", "REVIEW"}:
                raise MultipageStateError("WORKDAY_CONFIRMATION_REQUIRED", "Workday state accepts only confirmed page transitions")
            if (workday_navigation["expected_fingerprint"]
                    != stored_workday_navigation["current_fingerprint"]
                    or workday_navigation["visited_fingerprints"][:-1]
                    != stored_workday_navigation["visited_fingerprints"]
                    or workday_navigation["current_fingerprint"]
                    in stored_workday_navigation["visited_fingerprints"]):
                raise MultipageStateConflict("WORKDAY_OBSERVATION_STALE")
        elif workday_navigation is not None:
            raise MultipageStateError("WORKDAY_STATE_NOT_INITIALIZED", "Workday navigation baseline is missing")
        if page != current_page:
            raise MultipageStateError("INVALID_TRANSITION", "page_id does not match the current application page")
        if action == "SAVE_PROGRESS":
            if status != "IN_PROGRESS":
                raise MultipageStateError("INVALID_TRANSITION", "Review state must return to an application page before saving")
        elif action == "NEXT":
            if status != "IN_PROGRESS" or current_page not in order:
                raise MultipageStateError("INVALID_TRANSITION", "Next is unavailable from the current application state")
            index = order.index(current_page)
            destination = request["next_page_id"]
            if index + 1 >= len(order) or destination != order[index + 1]:
                raise MultipageStateError("INVALID_TRANSITION", "Next must follow the adjacent page in page_order")
            current_page = destination
        elif action == "PREVIOUS":
            if status == "REVIEW":
                expected_previous = order[-1]
                if request["previous_page_id"] != expected_previous:
                    raise MultipageStateError("INVALID_TRANSITION", "Previous must return from review to the final application page")
                current_page = expected_previous
                status = "IN_PROGRESS"
            else:
                if current_page not in order:
                    raise MultipageStateError("INVALID_TRANSITION", "Previous is unavailable from the current application state")
                index = order.index(current_page)
                if index == 0 or request["previous_page_id"] != order[index - 1]:
                    raise MultipageStateError("INVALID_TRANSITION", "Previous must follow the adjacent page in page_order")
                current_page = order[index - 1]
        elif action == "REVIEW":
            if status != "IN_PROGRESS" or current_page != order[-1]:
                raise MultipageStateError("INVALID_TRANSITION", "Review is available after the final application page")
            current_page = "review"
            status = "REVIEW"

    progress = _safe_progress(request["progress"], order)
    if action == "REVIEW" and current is None:
        raise MultipageStateError("INVALID_TRANSITION", "Review requires all application pages to be initialized")
    if action == "SAVE_PROGRESS" and current is not None:
        current_page = current["payload"]["state"]["current_page_id"]
    if action == "SAVE_PROGRESS" and current is None:
        current_page = request["page_id"]
    next_state = {
        "page_order": list(order),
        "current_page_id": current_page,
        "status": status,
        "progress": progress,
    }
    if workday_navigation is not None:
        next_state["workday_navigation"] = {
            "current_fingerprint": workday_navigation["current_fingerprint"],
            "visited_fingerprints": workday_navigation["visited_fingerprints"],
        }
    elif current is not None and "workday_navigation" in current["payload"]["state"]:
        next_state["workday_navigation"] = current["payload"]["state"]["workday_navigation"]
    return next_state


def transition_multipage_state(store, profile_id: str, application_id: str, payload: dict) -> dict:
    """Apply one versioned local page action and durably record its full snapshot."""
    request = _normalize_request(payload)
    if request["workday_navigation"] is not None:
        raise MultipageStateError(
            "WORKDAY_CONFIRMATION_REQUIRED",
            "Workday navigation evidence can be persisted only after adapter confirmation",
        )
    return _persist_multipage_request(store, profile_id, application_id, request)


def _persist_multipage_request(store, profile_id: str, application_id: str, request: dict) -> dict:
    """Persist a normalized request; Workday callers reach this only after confirmation."""
    entity_id = _entity_id(profile_id, application_id)
    request_event_id = _event_id(profile_id, application_id, request["request_id"])
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = _lock_and_load(connection, profile_id, application_id)
        if application["state"] in {"SUBMITTING", "CONFIRMED", "SUBMISSION_UNCONFIRMED"}:
            raise MultipageStateError("APPLICATION_NOT_EDITABLE", "Application page state is no longer editable")

        retry = connection.execute(select(m.audit).where(
            m.audit.c.id == request_event_id)).mappings().one_or_none()
        if retry is not None:
            if retry["entity_id"] != entity_id or retry["action"] != _EVENT_ACTION or retry["payload"].get("request") != request:
                raise MultipageStateConflict("MULTIPAGE_REQUEST_ID_CONFLICT")
            return _response(profile_id, application_id, retry)

        route_provider = connection.execute(select(m.routes.c.provider).where(
            m.routes.c.id == application["route_id"])).scalar_one_or_none()
        is_workday_route = isinstance(route_provider, str) and route_provider.casefold() == "workday"
        if is_workday_route and request["workday_navigation"] is None:
            raise MultipageStateError(
                "WORKDAY_OBSERVATION_REQUIRED",
                "Workday page transitions require confirmed observation evidence",
            )
        if request["workday_navigation"] is not None and not is_workday_route:
            raise MultipageStateError("WORKDAY_ROUTE_REQUIRED", "Workday state requires a Workday application route")

        current = _latest_snapshot(connection, entity_id)
        current_version = current["payload"]["state"]["version"] if current is not None else 0
        if request["expected_version"] != current_version:
            raise MultipageStateConflict()
        next_state = _validate_transition(current, request)
        next_state.update(version=current_version + 1, updated_at=now())
        connection.execute(m.audit.insert().values(
            id=request_event_id,
            entity_id=entity_id,
            action=_EVENT_ACTION,
            payload={"request": request, "state": next_state},
            timestamp=next_state["updated_at"],
        ))
        return _response(profile_id, application_id, {"payload": {"state": next_state}})


def _workday_manual_review(reason):
    return {"decision": "MANUAL_REVIEW", "reason": reason, "state": None}


def initialize_workday_multipage_state(store, profile_id: str, application_id: str,
                                       observation: dict, *, request_id: str) -> dict:
    """Persist a known initial Workday page as a versioned navigation baseline."""
    classified = classify_step(observation)
    if (classified["status"] != "KNOWN" or classified["stale"]
            or classified["step"] != _WORKDAY_PAGE_ORDER[0]):
        return _workday_manual_review("INITIAL_PAGE_NOT_CONFIRMED")
    request = {
        "request_id": request_id,
        "action": "SAVE_PROGRESS",
        "expected_version": 0,
        "page_id": _WORKDAY_PAGE_ORDER[0],
        "page_order": list(_WORKDAY_PAGE_ORDER),
        "progress": {},
        "workday_navigation": {
            "expected_fingerprint": None,
            "current_fingerprint": classified["fingerprint"],
            "visited_fingerprints": [classified["fingerprint"]],
        },
    }
    try:
        normalized_request = _normalize_request(request)
        state = _persist_multipage_request(store, profile_id, application_id, normalized_request)
    except MultipageStateError as error:
        return _workday_manual_review(error.code)
    return {"decision": "INITIALIZED", "reason": "KNOWN_INITIAL_PAGE", "state": state}


def transition_workday_multipage_state(store, profile_id: str, application_id: str,
                                        prior_state: dict, previous_observation: dict | None,
                                        current_observation: dict, intended_action: str, *,
                                        request_id: str) -> dict:
    """Persist only confirmed Workday observations through versioned multipage state."""
    if (not isinstance(prior_state, dict)
            or prior_state.get("profile_id") != profile_id
            or prior_state.get("application_id") != application_id
            or type(prior_state.get("version")) is not int
            or prior_state.get("version", 0) < 1
            or prior_state.get("page_order") != list(_WORKDAY_PAGE_ORDER)
            or not isinstance(prior_state.get("workday_navigation"), dict)):
        return _workday_manual_review("WORKDAY_STATE_NOT_INITIALIZED")

    navigation = prior_state["workday_navigation"]
    current = classify_step(current_observation)
    confirmed_state = {
        "status": "KNOWN",
        "step": prior_state.get("current_page_id"),
        "fingerprint": navigation.get("current_fingerprint"),
        "stale": False,
    }
    if intended_action == "RESUME":
        decision = decide_transition(
            previous_observation, current, intended_action,
            interrupted=True, confirmed_state=confirmed_state,
        )
        if decision["decision"] != "RESUME_FROM_CONFIRMED_STEP":
            return _workday_manual_review(decision["reason"])
        persisted = load_multipage_state(store, profile_id, application_id)
        if (persisted["version"] != prior_state["version"]
                or persisted.get("workday_navigation") != navigation
                or persisted["current_page_id"] != prior_state["current_page_id"]):
            return _workday_manual_review("MULTIPAGE_VERSION_CONFLICT")
        return {"decision": decision["decision"], "reason": decision["reason"], "state": persisted}

    previous = classify_step(previous_observation)
    decision = decide_transition(
        previous, current, intended_action,
        visited_fingerprints=navigation.get("visited_fingerprints", ()),
    )
    if decision["decision"] == "REFRESH_AND_RECLASSIFY":
        return {"decision": decision["decision"], "reason": decision["reason"], "state": None}
    if decision["decision"] != "CONFIRMED":
        return _workday_manual_review(decision["decision"])
    previous_page = "review" if previous["step"] == "review" else previous["step"]
    if (navigation.get("current_fingerprint") != previous["fingerprint"]
            or prior_state.get("current_page_id") != previous_page):
        return _workday_manual_review("DURABLE_STATE_MISMATCH")

    action = intended_action
    request = {
        "request_id": request_id,
        "action": action,
        "expected_version": prior_state["version"],
        "page_id": prior_state["current_page_id"],
        "progress": prior_state.get("progress", {}),
        "workday_navigation": {
            "expected_fingerprint": previous["fingerprint"],
            "current_fingerprint": current["fingerprint"],
            "visited_fingerprints": [*navigation.get("visited_fingerprints", []), current["fingerprint"]],
        },
    }
    if intended_action == "NEXT":
        if current["step"] == "review":
            request["action"] = "REVIEW"
        else:
            request["next_page_id"] = current["step"]
    else:
        request["previous_page_id"] = current["step"]
    try:
        normalized_request = _normalize_request(request)
        state = _persist_multipage_request(store, profile_id, application_id, normalized_request)
    except MultipageStateError as error:
        return _workday_manual_review(error.code)
    return {"decision": "CONFIRMED", "reason": decision["reason"],
            "fingerprint": current["fingerprint"], "state": state}
