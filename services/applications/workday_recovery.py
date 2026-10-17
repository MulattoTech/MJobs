"""Reconcile caller observations against durable Workday page checkpoints.

This coordinator never opens a page, authenticates, fills a form, or submits.
An accepted observation confirms only the locally recorded navigation step.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from packages.domain.operation_mode import WorkspaceModeError
from packages.persistence import models as m
from services.applications.multipage_state import (
    MultipageStateConflict,
    MultipageStateError,
    initialize_workday_multipage_state,
    load_multipage_state,
    transition_workday_multipage_state,
)


_ACTIONS = {"INITIALIZE", "NEXT", "PREVIOUS", "REFRESH", "RESUME"}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _assert_recoverable_application(store, profile_id, application_id):
    with store.engine.connect() as connection:
        row = connection.execute(select(
            m.applications.c.state, m.applications.c.job_id,
            m.routes.c.provider, m.routes.c.job_id.label("route_job_id"),
        ).select_from(m.applications.join(
            m.routes, m.applications.c.route_id == m.routes.c.id,
        )).where(
            m.applications.c.id == application_id,
            m.applications.c.profile_id == profile_id,
        )).mappings().one_or_none()
    if row is None:
        raise MultipageStateError("APPLICATION_NOT_FOUND", "Application was not found for this profile")
    if row["state"] in {"SUBMITTING", "CONFIRMED", "SUBMISSION_UNCONFIRMED"}:
        raise MultipageStateError("APPLICATION_NOT_EDITABLE", "Application page state is no longer editable")
    if str(row["provider"] or "").casefold() != "workday" or row["job_id"] != row["route_job_id"]:
        raise MultipageStateError("WORKDAY_ROUTE_REQUIRED", "Application requires its own Workday route")


def reconcile_workday_checkpoint(store, profile_id: str, application_id: str, *,
                                 action: str, request_id: str, expected_version: int,
                                 current_observation: dict,
                                 previous_observation: dict | None = None,
                                 operation_mode: str) -> dict:
    """Use current durable state, never a caller-provided snapshot, for recovery.

    A lost response is reconciled by reading state and resuming at its version.
    Stale mutations fail closed instead of re-executing a navigation action.
    """
    try:
        store.assert_workspace_mode(operation_mode)
    except WorkspaceModeError as error:
        raise MultipageStateError(error.code, "Workspace mode does not match this recovery request") from None
    if not isinstance(action, str) or action not in _ACTIONS:
        raise MultipageStateError("INVALID_ACTION", "Checkpoint action is not supported")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise MultipageStateError("INVALID_REQUEST_ID", "request_id must be a stable operation identifier")
    if type(expected_version) is not int or expected_version < 0:
        raise MultipageStateError("INVALID_VERSION", "expected_version must be a non-negative integer")

    prior = load_multipage_state(store, profile_id, application_id)
    _assert_recoverable_application(store, profile_id, application_id)
    if expected_version != prior["version"]:
        raise MultipageStateConflict()
    if action == "INITIALIZE":
        if expected_version != 0 or previous_observation is not None:
            raise MultipageStateError("INVALID_TRANSITION", "A checkpoint can only be initialized once")
        result = initialize_workday_multipage_state(
            store, profile_id, application_id, current_observation, request_id=request_id,
        )
    elif expected_version == 0:
        result = {"decision": "MANUAL_REVIEW", "reason": "WORKDAY_STATE_NOT_INITIALIZED", "state": None}
    else:
        result = transition_workday_multipage_state(
            store, profile_id, application_id, prior, previous_observation,
            current_observation, action, request_id=request_id,
        )
    return {**result, "execute": False, "manual_intervention_required": True}
