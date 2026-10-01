"""Bounded claims and safe recovery; application state owns submission truth."""
from datetime import datetime, timedelta, timezone
import os

from sqlalchemy import func, select, text, update

from packages.persistence import models as m
from packages.persistence.store import now

QUEUES = {"DISCOVER": "mjobs-discovery", "COMPANY_DISCOVER": "mjobs-discovery", "PREPARE": "mjobs-document", "APPLY_MOCK": "mjobs-application"}
MAX_ATTEMPTS = 3


class OperationCancelled(Exception):
    """Cooperative stop before an irreversible submission reservation."""


def timeout():
    seconds = int(os.environ.get("MJOBS_OPERATION_TIMEOUT", "180"))
    if not 30 <= seconds <= 600:
        raise ValueError("Operation timeout must be 30 through 600 seconds")
    return seconds


def limit(kind):
    count = int(os.environ.get("MJOBS_" + kind + "_CONCURRENCY", "1"))
    if not 1 <= count <= (2 if kind == "APPLY_MOCK" else 4):
        raise ValueError("Operation concurrency outside conservative bounds")
    return count


def _lock(connection, kind):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 649106700 + list(QUEUES).index(kind)})


def claim(store, operation_id):
    with store.engine.begin() as connection:
        kind = connection.execute(select(m.operations.c.kind).where(m.operations.c.id == operation_id)).scalar_one()
        _lock(connection, kind)
        row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id).with_for_update()).mappings().one()
        if row["state"] != "QUEUED" or row["cancel_requested"]:
            return None
        active = connection.execute(select(func.count()).select_from(m.operations).where(m.operations.c.kind == kind, m.operations.c.state == "RUNNING")).scalar_one()
        if active >= limit(kind):
            return None
        values = {"state": "RUNNING", "attempt_count": row["attempt_count"] + 1, "updated_at": now(),
                  "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=timeout() + 30)).isoformat()}
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(**values))
        return {**dict(row), **values}


def check_cancelled(store, operation_id):
    with store.engine.connect() as connection:
        row = connection.execute(select(m.operations.c.cancel_requested, m.operations.c.state).where(m.operations.c.id == operation_id)).one()
    if row.cancel_requested or row.state != "RUNNING":
        raise OperationCancelled("Operation no longer authorized to continue")


def request_cancel(store, operation_id):
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = dict(connection.execute(select(m.operations).where(m.operations.c.id == operation_id).with_for_update()).mappings().one())
        if row["kind"] == "APPLY_MOCK":
            application = connection.execute(select(m.applications).where(m.applications.c.id == row["entity_id"]).with_for_update()).mappings().one()
            if application["state"] in ("SUBMITTING", "SUBMISSION_UNCONFIRMED", "CONFIRMED"):
                return {**row, "reason": "RECONCILIATION_REQUIRED"}
        if row["state"] not in ("QUEUED", "RUNNING"):
            return {**row, "reason": "TERMINAL_OPERATION"}
        values = {"cancel_requested": True, "state": "CANCELLED" if row["state"] == "QUEUED" else row["state"], "updated_at": now()}
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(**values))
        store._audit(connection, operation_id, "CANCELLATION_REQUESTED", {})
        return {**row, **values}


def recover_expired(store):
    timestamp = now()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        rows = list(connection.execute(select(m.operations).where(m.operations.c.state == "RUNNING", m.operations.c.lease_expires_at <= timestamp).with_for_update(skip_locked=True)).mappings())
        for row in rows:
            state, error = ("QUEUED", "LEASE_EXPIRED") if row["attempt_count"] < MAX_ATTEMPTS else ("FAILED", "RETRY_LIMIT")
            if row["cancel_requested"]:
                state, error = "CANCELLED", "CANCEL_REQUESTED"
            if row["kind"] == "APPLY_MOCK":
                application = connection.execute(select(m.applications).where(m.applications.c.id == row["entity_id"]).with_for_update()).mappings().one()
                if application["state"] == "CONFIRMED":
                    state, error = "COMPLETED", None
                elif application["state"] in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
                    # Retain application state; this retry can only look up a receipt.
                    state, error = ("QUEUED", "RECONCILIATION_REQUIRED") if row["attempt_count"] < MAX_ATTEMPTS else ("BLOCKED", "RECONCILIATION_REQUIRED")
                elif application["state"] not in ("PREPARED", "DRY_RUN_COMPLETE"):
                    state, error = "BLOCKED", "PRE_SUBMIT_REVIEW_REQUIRED"
                    connection.execute(update(m.applications).where(m.applications.c.id == application["id"]).values(state="REVIEW_REQUIRED"))
            connection.execute(update(m.operations).where(m.operations.c.id == row["id"]).values(state=state, error=error, lease_expires_at=None, updated_at=timestamp))
            store._audit(connection, row["id"], "LEASE_RECOVERY", {"state": state, "error": error})
        return len(rows)
