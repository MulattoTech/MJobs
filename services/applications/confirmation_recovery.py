"""Explicit read-only MOCK receipt reconciliation; never launches or submits.

A missing/unverifiable receipt is NOT evidence that submission did not happen.
The durable original attempt remains untouched when a later check succeeds.
"""
from __future__ import annotations

import re
import time
from sqlalchemy import select, update
from sqlalchemy.orm.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity, now
from services.discovery.pipeline import MockClient

ACTION = "APPLICATION_CONFIRMATION_CHECKED_V1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ConfirmationConflict(ValueError):
    pass


def _application(connection, profile_id, application_id, *, lock=False):
    query = select(m.applications).where(m.applications.c.id == application_id,
                                        m.applications.c.profile_id == profile_id)
    if lock:
        query = query.with_for_update()
    row = connection.execute(query).mappings().one_or_none()
    if row is None:
        raise NoResultFound("APPLICATION_NOT_FOUND")
    return dict(row)


def _route(connection, application):
    return dict(connection.execute(select(m.routes).where(
        m.routes.c.id == application["route_id"])).mappings().one())


def _guard(connection, application, route):
    if (application["target"] != "MOCK" or route["provider"] != "mock-ats"
            or route.get("tenant") != "example"):
        raise ConfirmationConflict("CONFIRMATION_TARGET_UNSUPPORTED")
    if application["state"] not in {"SUBMITTING", "SUBMISSION_UNCONFIRMED", "CONFIRMED"}:
        raise ConfirmationConflict("CONFIRMATION_STATE_NOT_ELIGIBLE")
    active_attempt = connection.execute(select(m.attempts.c.id).where(
        m.attempts.c.application_id == application["id"], m.attempts.c.state == "RUNNING").limit(1)).first()
    active_operation = connection.execute(select(m.operations.c.id).where(
        m.operations.c.kind == "APPLY_MOCK", m.operations.c.entity_id == application["id"],
        m.operations.c.state == "RUNNING").limit(1)).first()
    if active_attempt is not None or active_operation is not None:
        raise ConfirmationConflict("CONFIRMATION_EXECUTION_STILL_RESERVED")


def _previous(connection, event_id, application):
    event = connection.execute(select(m.audit.c.payload).where(
        m.audit.c.id == event_id, m.audit.c.entity_id == application["id"],
        m.audit.c.action == ACTION)).scalar_one_or_none()
    if event is None:
        return None
    if event.get("route_id") != application["route_id"]:
        raise ConfirmationConflict("CONFIRMATION_ROUTE_CHANGED")
    return {**event["result"], "replayed": True}


def reconcile_confirmation(store, profile_id, application_id, mock_origin,
                           request_id, *, operation_mode):
    if operation_mode != "FIXTURE":
        raise ConfirmationConflict("CONFIRMATION_FIXTURE_ONLY")
    try:
        store.assert_workspace_mode("FIXTURE")
    except ValueError:
        raise ConfirmationConflict("CONFIRMATION_FIXTURE_ONLY") from None
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ConfirmationConflict("CONFIRMATION_REQUEST_INVALID")
    event_id = identity("confirmation-check-v1", application_id, request_id)
    with store.engine.connect() as connection:
        application = _application(connection, profile_id, application_id)
        prior = _previous(connection, event_id, application)
        if prior is not None:
            return prior
        route = _route(connection, application)
        _guard(connection, application, route)
    deadline = time.monotonic() + 20
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("CONFIRMATION_READ_TIMEOUT")
    # The same validated local-origin receipt contract used by the browser flow.
    # No POST, browser, authentication bypass, or retry of the submit step exists.
    from services.applications.mock_adapter import _receipt
    client = MockClient(mock_origin, check=check)
    try:
        if not client.allowed(route["url"]):
            raise ConfirmationConflict("CONFIRMATION_ORIGIN_MISMATCH")
        receipt = _receipt(client, application, route["requisition"])
    finally:
        client.close()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = _application(connection, profile_id, application_id, lock=True)
        prior = _previous(connection, event_id, current)
        if prior is not None:
            return prior
        current_route = _route(connection, current)
        if current["route_id"] != application["route_id"] or any(
                current_route.get(key) != route.get(key)
                for key in ("provider", "tenant", "requisition", "url")):
            raise ConfirmationConflict("CONFIRMATION_ROUTE_CHANGED")
        _guard(connection, current, current_route)
        # Do not downgrade an already confirmed application on a later outage.
        already_confirmed = current["state"] == "CONFIRMED"
        resolved_receipt = current.get("confirmation") if already_confirmed else receipt
        confirmed = already_confirmed or receipt is not None
        state = "CONFIRMED" if confirmed else "SUBMISSION_UNCONFIRMED"
        timestamp = now()
        public_receipt = None
        if isinstance(resolved_receipt, dict):
            public_receipt = {key: resolved_receipt.get(key)
                              for key in ("reference_number", "timestamp")}
        result = {"check_id": event_id, "profile_id": profile_id,
                  "application_id": application_id,
                  "status": "CONFIRMED" if confirmed else "NO_VERIFIED_RECEIPT",
                  "application_state": state, "checked_at": timestamp,
                  "confirmation": public_receipt,
                  "provider_receipt_verified": receipt is not None,
                  "confirmation_source": "RECEIPT_LOOKUP" if receipt is not None else (
                      "PREVIOUS_CONFIRMATION" if already_confirmed else "NONE"),
                  "replayed": False, "provider_method": "GET",
                  "browser_started": False, "submission_attempted": False}
        if state != current["state"]:
            values = {"state": state, "trace": [*(current.get("trace") or []),
                       "RECONCILE_CONFIRMATION_READ_ONLY"]}
            if receipt is not None:
                values["confirmation"] = receipt
            updated = connection.execute(update(m.applications).where(
                m.applications.c.id == application_id,
                m.applications.c.state == current["state"]).values(**values))
            if updated.rowcount != 1:
                raise ConfirmationConflict("CONFIRMATION_STATE_CHANGED")
        connection.execute(m.audit.insert().values(
            id=event_id, entity_id=application_id, action=ACTION, timestamp=timestamp,
            payload={"schema_version": 1, "route_id": current["route_id"], "result": result}))
    return result
