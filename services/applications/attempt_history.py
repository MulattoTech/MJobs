"""Immutable, privacy-bounded execution receipts over the existing attempt schema.

The application row serializes admission. A running attempt is never stolen, even
when its worker disappears. Legacy attempts remain explicitly incomplete; history
is not backfilled from today's route, profile, or application state.
"""
from __future__ import annotations

import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select, update
from sqlalchemy.orm.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity, now

STARTED = "APPLICATION_ATTEMPT_STARTED_V1"
FINISHED = "APPLICATION_ATTEMPT_FINISHED_V1"
_PHASES = frozenset({
    "NAVIGATE", "DETECT_FORM", "UPLOAD_RESUME", "UPLOAD_SUPPORTING_DOCUMENTS",
    "FILL_CONTACT", "FILL_LOCATION", "ANSWER_STANDARD", "ANSWER_CUSTOM",
    "VALIDATE", "SUBMIT", "VERIFY_CONFIRMATION",
})
_STATUSES = frozenset({"STARTED", "COMPLETED", "FAILED", "BLOCKED"})
_EDITABLE = frozenset({"PREPARED", "DRY_RUN_COMPLETE", "REVIEW_REQUIRED", "AUTH_REQUIRED"})
_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AttemptConflict(ValueError):
    """A fixed public error code, never a browser exception or form value."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _lock(connection):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _event_id(action, attempt_id):
    return identity("application-attempt-event-v1", action, attempt_id)


def _steps(values):
    if not isinstance(values, list):
        return []
    return [value for value in values[-100:]
            if isinstance(value, str) and value.count(":") == 1
            and value.split(":")[0] in _PHASES and value.split(":")[1] in _STATUSES]


def input_snapshot(application, route, documents, *, submit=False, supporting_documents=()):
    """Stable v1 input contract shared by queue admission and browser reservation."""
    snapshot = {
        "route_id": application["route_id"],
        "provider": route["provider"], "tenant": route.get("tenant"),
        "requisition": route.get("requisition"),
        "route_url_sha256": hashlib.sha256(route["url"].encode()).hexdigest(),
        "profile_id": application["profile_id"],
        "profile_version": application["profile_version"],
        "document_version": application["document_version"],
        "documents": sorted([{"id": row["id"], "kind": row["kind"],
                               "sha256": row["sha256"]} for row in documents],
                            key=lambda row: (row["kind"], row["id"])),
        "supporting_documents": sorted([
            {"revision_id": item["revision_id"], "sha256": item["sha256"],
             "job_id": item["job_id"]} for item in supporting_documents
        ], key=lambda item: item["revision_id"]),
        "mode": "MOCK_SUBMISSION" if submit else "DRY_RUN",
    }
    return snapshot


def input_fingerprint(snapshot):
    return hashlib.sha256(json.dumps(
        snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def attempt_id_for_request(application_id, request_id):
    return uuid5(NAMESPACE_URL, identity(
        "application-attempt-v1", application_id, request_id)).hex


def start_attempt(store, application, route, documents, *, submit=False, request_id=None,
                  supporting_documents=(), expected_fingerprint=None):
    """Atomically reserve one physical browser execution; replay never opens it.

    Stable caller request IDs are scoped to an application and immutable inputs.
    A retry with different inputs fails closed. A genuinely new execution requires
    a new request ID. Running and failed replays are explicit conflicts.
    """
    request_id = uuid4().hex if request_id is None else request_id
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        raise AttemptConflict("APPLICATION_ATTEMPT_REQUEST_INVALID")
    if type(submit) is not bool:
        raise AttemptConflict("APPLICATION_ATTEMPT_MODE_INVALID")
    if application["target"] != "MOCK" or route["provider"] != "mock-ats":
        raise AttemptConflict("APPLICATION_ATTEMPT_TARGET_UNSUPPORTED")
    snapshot = input_snapshot(application, route, documents, submit=submit,
                              supporting_documents=supporting_documents)
    fingerprint = input_fingerprint(snapshot)
    if expected_fingerprint is not None and expected_fingerprint != fingerprint:
        raise AttemptConflict("APPLICATION_RUN_INPUT_CHANGED")
    # Keep the existing 32-hex attempt-ID contract used by private debug capture.
    if any(item["job_id"] != application["job_id"] for item in supporting_documents):
        raise AttemptConflict("APPLICATION_ATTEMPT_SUPPORTING_SCOPE_MISMATCH")
    attempt_id = attempt_id_for_request(application["id"], request_id)
    with store.engine.begin() as connection:
        _lock(connection)
        current = connection.execute(select(m.applications).where(
            m.applications.c.id == application["id"]).with_for_update()).mappings().one()
        for key in ("route_id", "profile_id", "profile_version", "document_version", "target"):
            if current[key] != application[key]:
                raise AttemptConflict("APPLICATION_ATTEMPT_INPUT_CHANGED")
        live_route = connection.execute(select(m.routes).where(
            m.routes.c.id == current["route_id"])).mappings().one()
        if any(live_route.get(key) != route.get(key)
               for key in ("provider", "tenant", "requisition", "url")):
            raise AttemptConflict("APPLICATION_ATTEMPT_INPUT_CHANGED")
        live_documents = sorted([
            {"id": row["id"], "kind": row["kind"], "sha256": row["sha256"]}
            for row in connection.execute(select(m.documents).where(
                m.documents.c.application_id == current["id"],
                m.documents.c.version == current["document_version"])).mappings()
        ], key=lambda row: (row["kind"], row["id"]))
        if live_documents != snapshot["documents"]:
            raise AttemptConflict("APPLICATION_ATTEMPT_INPUT_CHANGED")
        if expected_fingerprint is not None:
            # Recheck the queue snapshot under the same admission lock, including
            # supporting selection changed during browser preflight.
            from services.applications.run_requests import current_input_snapshot
            if input_fingerprint(current_input_snapshot(connection, current, submit=submit)) != expected_fingerprint:
                raise AttemptConflict("APPLICATION_RUN_INPUT_CHANGED")
        previous = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.id == _event_id(STARTED, attempt_id))).scalar_one_or_none()
        if previous is not None:
            if previous.get("input_fingerprint") != fingerprint:
                raise AttemptConflict("APPLICATION_ATTEMPT_REQUEST_REUSED")
            saved = connection.execute(select(m.attempts.c.state).where(
                m.attempts.c.id == attempt_id)).scalar_one()
            if saved == "RUNNING":
                raise AttemptConflict("APPLICATION_ATTEMPT_IN_PROGRESS")
            if saved not in {"CONFIRMED", "DRY_RUN_COMPLETE"}:
                raise AttemptConflict("APPLICATION_ATTEMPT_REVIEW_REQUIRED")
            return {"id": attempt_id, "replayed": True}
        if current["state"] not in _EDITABLE:
            raise AttemptConflict("APPLICATION_ATTEMPT_STATE_CHANGED")
        active = connection.execute(select(m.attempts.c.id).where(
            m.attempts.c.application_id == application["id"],
            m.attempts.c.state == "RUNNING").limit(1)).first()
        if active is not None:
            raise AttemptConflict("APPLICATION_ATTEMPT_IN_PROGRESS")
        timestamp = now()
        connection.execute(m.attempts.insert().values(
            id=attempt_id, application_id=application["id"], state="RUNNING",
            steps=[], started_at=timestamp))
        connection.execute(m.audit.insert().values(
            id=_event_id(STARTED, attempt_id), entity_id=application["id"],
            action=STARTED, timestamp=timestamp,
            payload={"schema_version": 1, "attempt_id": attempt_id,
                     "adapter": "mock-ats", "input_fingerprint": fingerprint,
                     "browser_session": identity("mock-browser-profile", application["id"]),
                     "snapshot": snapshot}))
    return {"id": attempt_id, "replayed": False}


def failure_code(error):
    if error is None:
        return None
    # Do not persist error messages: browser exceptions can contain user values,
    # local paths, query strings, or credentials.
    return {"OperationCancelled": "OPERATION_CANCELLED",
            "OperationTimedOut": "OPERATION_TIMEOUT",
            "TimeoutError": "BROWSER_TIMEOUT",
            "UploadIntegrityError": "UPLOAD_INTEGRITY_FAILED"}.get(type(error).__name__,
                                                   "APPLICATION_EXECUTION_FAILED")


def finish_attempt(store, attempt_id, application, *, error=None):
    """Close an execution without rewriting its earlier immutable input receipt."""
    with store.engine.begin() as connection:
        _lock(connection)
        row = connection.execute(select(m.attempts).where(
            m.attempts.c.id == attempt_id).with_for_update()).mappings().one()
        if row["application_id"] != application["id"]:
            raise AttemptConflict("APPLICATION_ATTEMPT_SCOPE_MISMATCH")
        existing = connection.execute(select(m.audit.c.id).where(
            m.audit.c.id == _event_id(FINISHED, attempt_id))).first()
        if existing is not None:
            return  # Immutable first completion wins; never update it later.
        steps = _steps(row["steps"])
        state = application["state"]
        if error is not None and state not in {
                "CONFIRMED", "SUBMISSION_UNCONFIRMED", "REVIEW_REQUIRED",
                "AUTH_REQUIRED", "USER_INTERVENTION_REQUIRED"}:
            state = "FAILED"
        code = failure_code(error)
        if code is None and state in {"REVIEW_REQUIRED", "AUTH_REQUIRED",
                                     "USER_INTERVENTION_REQUIRED", "SUBMISSION_UNCONFIRMED"}:
            code = state
        confirmation = application.get("confirmation") or {}
        receipt = None
        if state == "CONFIRMED" and isinstance(confirmation, dict):
            reference = confirmation.get("reference_number")
            if isinstance(reference, str) and reference.startswith("MOCK-") and len(reference) <= 200:
                receipt = {"reference_number": reference,
                           "timestamp": confirmation.get("timestamp")}
        timestamp = now()
        payload = {"schema_version": 1, "attempt_id": attempt_id,
                   "state": state, "finished_at": timestamp,
                   "current_step": steps[-1].split(":")[0] if steps else None,
                   "error_code": code, "confirmation": receipt}
        connection.execute(update(m.attempts).where(
            m.attempts.c.id == attempt_id).values(state=state))
        connection.execute(m.audit.insert().values(
            id=_event_id(FINISHED, attempt_id), entity_id=row["application_id"],
            action=FINISHED, payload=payload, timestamp=timestamp))


def read_attempt_history(store, profile_id, application_id):
    """Bounded read-only profile-scoped view, with honest legacy/null metadata."""
    with store.engine.connect() as connection:
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id,
            m.applications.c.profile_id == profile_id)).mappings().one_or_none()
        if application is None:
            raise NoResultFound("APPLICATION_NOT_FOUND")
        rows = list(connection.execute(select(m.attempts).where(
            m.attempts.c.application_id == application_id).order_by(
                m.attempts.c.started_at.desc(), m.attempts.c.id.desc()).limit(101)).mappings())
        event_ids = [_event_id(action, row["id"]) for row in rows[:100]
                     for action in (STARTED, FINISHED)]
        from services.applications.upload_integrity import ACTION as UPLOAD_ACTION
        event_ids.extend(identity(UPLOAD_ACTION, row["id"]) for row in rows[:100])
        events = {row["id"]: row["payload"] for row in connection.execute(
            select(m.audit.c.id, m.audit.c.payload).where(m.audit.c.id.in_(event_ids))).mappings()}
        result = []
        for row in rows[:100]:
            started = events.get(_event_id(STARTED, row["id"]), {})
            finished = events.get(_event_id(FINISHED, row["id"]), {})
            snapshot = started.get("snapshot", {})
            steps = _steps(row["steps"])
            result.append({
                "attempt_id": row["id"], "application_id": application_id,
                "route_id": snapshot.get("route_id"), "state": row["state"],
                "started_at": row["started_at"], "finished_at": finished.get("finished_at"),
                "adapter": started.get("adapter"),
                "browser_session": started.get("browser_session"),
                "current_step": steps[-1].split(":")[0] if steps else None,
                "error_code": finished.get("error_code"),
                "confirmation": finished.get("confirmation"),
                "input_fingerprint": started.get("input_fingerprint"),
                "profile_version": snapshot.get("profile_version"),
                "document_version": snapshot.get("document_version"),
                "mode": snapshot.get("mode"), "steps": steps,
                "legacy_metadata": not bool(started),
                "verified_uploads": events.get(identity(UPLOAD_ACTION, row["id"]), {}).get("documents", []),
            })
    return {"profile_id": profile_id, "application_id": application_id,
            "attempts": result, "has_more": len(rows) > 100,
            "external_submission_enabled": False}
