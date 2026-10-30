"""Review-first mock run requests, durable across lost replies and worker restarts.

This is database admission, not submission authorization for an external ATS.
One explicit caller intent owns one operation and one browser-attempt identity.
The existing audit/outbox schema is sufficient; no reservations are taken over.
"""
from __future__ import annotations

import re
from sqlalchemy import select
from sqlalchemy.orm.exc import NoResultFound

from packages.domain.operation_types import validate_operation_type
from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.applications.attempt_history import (
    AttemptConflict, attempt_id_for_request, input_snapshot, input_fingerprint,
)

_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_EDITABLE = frozenset({"PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED", "AUTH_REQUIRED"})


def _identifier(profile_id, application_id, request_id):
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise AttemptConflict("APPLICATION_RUN_REQUEST_INVALID")
    return identity("application-run-request-v1", profile_id, application_id, request_id)


def _application(connection, profile_id, application_id, *, lock=False):
    query = select(m.applications).where(
        m.applications.c.id == application_id, m.applications.c.profile_id == profile_id)
    if lock:
        query = query.with_for_update()
    row = connection.execute(query).mappings().one_or_none()
    if row is None:
        raise NoResultFound("APPLICATION_NOT_FOUND")
    if row["target"] != "MOCK":
        raise AttemptConflict("APPLICATION_RUN_TARGET_UNSUPPORTED")
    return dict(row)


def current_input_snapshot(connection, application, *, submit):
    """Snapshot metadata in the caller's transaction; no file or network access."""
    from services.applications.supporting_documents import _selection_view, _selection_events, _latest
    route = connection.execute(select(m.routes).where(
        m.routes.c.id == application["route_id"])).mappings().one()
    if route["provider"] != "mock-ats":
        raise AttemptConflict("APPLICATION_RUN_TARGET_UNSUPPORTED")
    documents = list(connection.execute(select(m.documents).where(
        m.documents.c.application_id == application["id"],
        m.documents.c.version == application["document_version"])).mappings())
    selection = _selection_view(connection, application["profile_id"], application["id"])
    if selection["unavailable_revision_ids"]:
        raise AttemptConflict("APPLICATION_RUN_SUPPORTING_UNAVAILABLE")
    event = _latest(_selection_events(connection, application["id"]))
    payload = (event or {}).get("payload") or {}
    items = payload.get("items", [])
    if items and any(payload.get(key) != value for key, value in {
        "application_id": application["id"], "profile_id": application["profile_id"],
        "job_id": application["job_id"],
    }.items()):
        raise AttemptConflict("APPLICATION_RUN_SUPPORTING_SCOPE_MISMATCH")
    supporting = [{"revision_id": item["revision_id"], "sha256": item["sha256"],
                   "job_id": application["job_id"]} for item in items]
    return input_snapshot(application, route, documents, submit=submit, supporting_documents=supporting)


def _view(operation, *, replayed):
    return {"operation_id": operation["id"], "state": operation["state"],
            "error_code": operation.get("error"), "replayed": replayed,
            "input_fingerprint": operation["params"].get("input_fingerprint"),
            "external_submission_enabled": False}


def queue_mock_run(store, profile_id, application_id, *, submit=False, request_id,
                   resume_binding=None):
    """Admit a new reviewed intent, or return the same immutable durable request."""
    validate_operation_type("APPLY_MOCK")
    operation_id = _identifier(profile_id, application_id, request_id)
    if type(submit) is not bool:
        raise AttemptConflict("APPLICATION_RUN_MODE_INVALID")
    store.assert_workspace_mode("FIXTURE")
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        application = _application(connection, profile_id, application_id, lock=True)
        snapshot = current_input_snapshot(connection, application, submit=submit)
        fingerprint = input_fingerprint(snapshot)
        previous = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id)).mappings().one_or_none()
        if previous is not None:
            if (previous["kind"] != "APPLY_MOCK" or previous["entity_id"] != application_id
                    or previous["params"].get("input_fingerprint") != fingerprint
                    or previous["params"].get("input_snapshot") != snapshot
                    or previous["params"].get("resume_binding") != resume_binding):
                raise AttemptConflict("APPLICATION_RUN_REQUEST_REUSED")
            return _view(previous, replayed=True)
        if application["state"] in {"SUBMITTING", "SUBMISSION_UNCONFIRMED"}:
            raise AttemptConflict("RECONCILIATION_REQUIRED")
        if application["state"] == "CONFIRMED":
            raise AttemptConflict("APPLICATION_ALREADY_CONFIRMED")
        active_attempt = connection.execute(select(m.attempts.c.id).where(
            m.attempts.c.application_id == application_id, m.attempts.c.state == "RUNNING").limit(1)).first()
        if active_attempt is not None:
            raise AttemptConflict("APPLICATION_ATTEMPT_IN_PROGRESS")
        if application["state"] not in _EDITABLE:
            raise AttemptConflict("APPLICATION_REVIEW_REQUIRED")
        active_operation = connection.execute(select(m.operations.c.id).where(
            m.operations.c.entity_id == application_id, m.operations.c.kind == "APPLY_MOCK",
            m.operations.c.state.in_(("QUEUED", "RUNNING"))).limit(1)).first()
        if active_operation is not None:
            raise AttemptConflict("APPLICATION_RUN_IN_PROGRESS")
        timestamp = now()
        row = {"id": operation_id, "kind": "APPLY_MOCK", "entity_id": application_id,
               "params": {"submit": submit, "run_contract_version": 1,
                          "input_fingerprint": fingerprint, "input_snapshot": snapshot,
                          **({"resume_binding": resume_binding, "request_id": request_id}
                             if resume_binding is not None else {})},
               "state": "QUEUED", "error": None, "updated_at": timestamp}
        connection.execute(m.operations.insert().values(**row))
        store._audit(connection, application_id, "APPLICATION_RUN_REQUESTED_V1", {
            "operation_id": operation_id, "input_fingerprint": fingerprint,
            "mode": snapshot["mode"],
        })
        return _view(row, replayed=False)


def read_mock_run(store, profile_id, application_id, request_id):
    """Recover a lost acknowledgement by scoped request ID, without queueing."""
    operation_id = _identifier(profile_id, application_id, request_id)
    store.assert_workspace_mode("FIXTURE")
    with store.engine.connect() as connection:
        _application(connection, profile_id, application_id)
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id, m.operations.c.entity_id == application_id,
            m.operations.c.kind == "APPLY_MOCK")).mappings().one_or_none()
        if row is None:
            raise NoResultFound("APPLICATION_RUN_NOT_FOUND")
        return _view(row, replayed=True)


def _validate_inputs(connection, operation, application):
    params = operation["params"]
    if "run_contract_version" not in params:
        return  # Legacy requests remain readable; they have no frozen queue snapshot.
    if type(params["run_contract_version"]) is not int or params["run_contract_version"] != 1:
        raise AttemptConflict("APPLICATION_RUN_CONTRACT_UNSUPPORTED")
    if (type(params.get("submit")) is not bool
            or not isinstance(params.get("input_snapshot"), dict)
            or input_fingerprint(params["input_snapshot"]) != params.get("input_fingerprint")):
        raise AttemptConflict("APPLICATION_RUN_INPUT_CHANGED")
    if current_input_snapshot(connection, application, submit=params["submit"]) != params["input_snapshot"]:
        raise AttemptConflict("APPLICATION_RUN_INPUT_CHANGED")


def validate_run_operation(store, operation):
    """Fail before provider access if the queued reviewed inputs changed."""
    if "run_contract_version" not in operation["params"]:
        return  # Legacy adapter retains its own original application validation.
    with store.engine.connect() as connection:
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == operation["entity_id"])).mappings().one()
        _validate_inputs(connection, operation, application)


def application_retry_blocker(connection, operation, *, lock=True):
    """Shared manual-retry/recovery/read-model policy; never close a live attempt."""
    query = select(m.applications).where(m.applications.c.id == operation["entity_id"])
    application = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if application is None:
        return "APPLICATION_NOT_FOUND"
    if application["state"] in {"SUBMITTING", "SUBMISSION_UNCONFIRMED"}:
        return "RECONCILIATION_REQUIRED"
    if application["state"] == "CONFIRMED":
        return "APPLICATION_ALREADY_CONFIRMED"
    if connection.execute(select(m.attempts.c.id).where(
            m.attempts.c.application_id == application["id"],
            m.attempts.c.state == "RUNNING").limit(1)).first() is not None:
        return "APPLICATION_ATTEMPT_IN_PROGRESS"
    if application["state"] not in {"PREPARED", "DRY_RUN_COMPLETE"}:
        return "APPLICATION_REVIEW_REQUIRED"
    if connection.execute(select(m.operations.c.id).where(
            m.operations.c.entity_id == application["id"],
            m.operations.c.kind == "APPLY_MOCK", m.operations.c.id != operation["id"],
            m.operations.c.state.in_(("QUEUED", "RUNNING"))).limit(1)).first() is not None:
        return "APPLICATION_RUN_IN_PROGRESS"
    previous = connection.execute(select(m.attempts.c.state).where(
        m.attempts.c.id == attempt_id_for_request(application["id"], operation["id"]))).scalar_one_or_none()
    if previous is not None and previous not in {"DRY_RUN_COMPLETE", "CONFIRMED"}:
        return "APPLICATION_ATTEMPT_REVIEW_REQUIRED"
    try:
        _validate_inputs(connection, operation, application)
    except AttemptConflict as error:
        return error.code
    return None
