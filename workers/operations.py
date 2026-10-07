"""Bounded claims and safe recovery; application state owns submission truth."""
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

from sqlalchemy import func, select, text, update

from packages.persistence import models as m
from packages.persistence.store import now

QUEUES = {"DISCOVER": "mjobs-discovery", "COMPANY_DISCOVER": "mjobs-discovery",
          "COMPANY_SOURCE_REFRESH": "mjobs-discovery", "PREPARE": "mjobs-document",
          "APPLY_MOCK": "mjobs-application"}
OPERATION_CATEGORIES = {
    "DISCOVER": "source_collection",
    "COMPANY_DISCOVER": "company_discovery",
    "COMPANY_SOURCE_REFRESH": "company_discovery",
    "AI_EVALUATE": "ai",
    "RESUME_EXTRACT": "document_generation",
    "UPLOADED_RESUME_COMPARE": "document_generation",
    "PREPARE": "document_generation",
    "APPLY_MOCK": "application_automation",
}
CATEGORY_LIMITS = {
    "source_collection": (2, 4),
    "company_discovery": (1, 2),
    "ai": (1, 2),
    "document_generation": (1, 2),
    "application_automation": (1, 2),
}
CATEGORY_KINDS = {
    category: tuple(kind for kind, operation_category in OPERATION_CATEGORIES.items()
                    if operation_category == category)
    for category in CATEGORY_LIMITS
}
CATEGORIES = tuple(CATEGORY_LIMITS)
MAX_ATTEMPTS = 3
INLINE_KINDS = ("AI_EVALUATE", "RESUME_EXTRACT", "UPLOADED_RESUME_COMPARE")


class OperationCancelled(Exception):
    """Cooperative stop before an irreversible submission reservation."""


class OperationTimedOut(TimeoutError):
    """The worker operation exceeded its configured monotonic time budget."""


class OperationConcurrencyLimit(Exception):
    """The configured category concurrency ceiling is currently occupied."""


class OperationActionError(ValueError):
    """A manual operation action is not valid for the persisted state."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def timeout():
    seconds = int(os.environ.get("MJOBS_OPERATION_TIMEOUT", "180"))
    if not 30 <= seconds <= 600:
        raise ValueError("Operation timeout must be 30 through 600 seconds")
    return seconds


def category_limit(category):
    if category not in CATEGORY_LIMITS:
        raise ValueError("Unsupported operation concurrency category")
    default, maximum = CATEGORY_LIMITS[category]
    try:
        count = int(os.environ.get("MJOBS_" + category.upper() + "_CONCURRENCY", str(default)))
    except (TypeError, ValueError) as error:
        raise ValueError("Operation concurrency outside conservative bounds") from error
    if not 1 <= count <= maximum:
        raise ValueError("Operation concurrency outside conservative bounds")
    return count


def limit(kind):
    try:
        category = OPERATION_CATEGORIES[kind]
    except KeyError as error:
        raise ValueError("Unsupported operation kind") from error
    return category_limit(category)


def _lock(connection, category):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                           {"key": 649106700 + CATEGORIES.index(category)})


def claim(store, operation_id):
    with store.engine.begin() as connection:
        kind = connection.execute(select(m.operations.c.kind).where(m.operations.c.id == operation_id)).scalar_one()
        category = OPERATION_CATEGORIES[kind]
        _lock(connection, category)
        row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id).with_for_update()).mappings().one()
        if row["state"] != "QUEUED" or row["cancel_requested"]:
            return None
        active = connection.execute(select(func.count()).select_from(m.operations).where(
            m.operations.c.kind.in_(CATEGORY_KINDS[category]), m.operations.c.state == "RUNNING")).scalar_one()
        if active >= limit(kind):
            return None
        timestamp = now()
        values = {"state": "RUNNING", "attempt_count": row["attempt_count"] + 1,
                  "last_attempt_at": timestamp, "updated_at": timestamp,
                  "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=timeout() + 30)).isoformat()}
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(**values))
        store._audit(connection, operation_id, "OPERATION_ATTEMPT_STARTED",
                     {"attempt_count": values["attempt_count"]})
        return {**dict(row), **values}


def admit_inline(store, kind, entity_id):
    """Reserve a bounded operation that executes in the API process, not RQ."""
    if kind not in INLINE_KINDS or kind in QUEUES:
        raise ValueError("Unsupported inline operation kind")
    category = OPERATION_CATEGORIES[kind]
    timestamp = now()
    operation_id = uuid4().hex
    with store.engine.begin() as connection:
        _lock(connection, category)
        # An API process may exit without a completion callback. Inline
        # operations are never retried: an expired lease only frees capacity.
        connection.execute(update(m.operations).where(
            m.operations.c.kind.in_(INLINE_KINDS),
            m.operations.c.state == "RUNNING",
            m.operations.c.lease_expires_at <= timestamp,
        ).values(state="FAILED", error="LEASE_EXPIRED", lease_expires_at=None,
                 updated_at=timestamp))
        active = connection.execute(select(func.count()).select_from(m.operations).where(
            m.operations.c.kind.in_(CATEGORY_KINDS[category]),
            m.operations.c.state == "RUNNING")).scalar_one()
        if active >= category_limit(category):
            raise OperationConcurrencyLimit(category)
        connection.execute(m.operations.insert().values(
            id=operation_id, kind=kind, entity_id=entity_id, params={},
            state="RUNNING", error=None, updated_at=timestamp, attempt_count=1,
            cancel_requested=False,
            lease_expires_at=(datetime.now(timezone.utc)
                              + timedelta(seconds=timeout() + 30)).isoformat(),
        ))
    return operation_id


def finish_inline(store, operation_id, state, error=None):
    if state not in ("COMPLETED", "CANCELLED", "FAILED"):
        raise ValueError("Invalid inline operation terminal state")
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(select(
            m.operations.c.state, m.operations.c.cancel_requested,
        ).where(m.operations.c.id == operation_id,
                m.operations.c.kind.in_(INLINE_KINDS)).with_for_update()).one_or_none()
        if row is None or row.state != "RUNNING":
            return False
        if row.cancel_requested and state == "COMPLETED":
            state, error = "CANCELLED", "CANCEL_REQUESTED"
        connection.execute(update(m.operations).where(
            m.operations.c.id == operation_id,
            m.operations.c.kind.in_(INLINE_KINDS),
            m.operations.c.state == "RUNNING",
        ).values(state=state, error=error, updated_at=now(), lease_expires_at=None))
        return True


def inline_cancel_requested(store, operation_id):
    with store.engine.connect() as connection:
        row = connection.execute(select(
            m.operations.c.cancel_requested, m.operations.c.state,
        ).where(m.operations.c.id == operation_id,
                m.operations.c.kind.in_(INLINE_KINDS))).one_or_none()
    return row is None or row.cancel_requested or row.state != "RUNNING"


def check_cancelled(store, operation_id, attempt_count=None):
    with store.engine.connect() as connection:
        row = connection.execute(select(
            m.operations.c.cancel_requested, m.operations.c.state,
            m.operations.c.attempt_count,
        ).where(m.operations.c.id == operation_id)).one()
    if (row.cancel_requested or row.state != "RUNNING"
            or (attempt_count is not None and row.attempt_count != attempt_count)):
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


def retry_dead_letter(store, operation_id):
    """Explicitly requeue a dead letter without changing its idempotency identity."""
    timestamp = now()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id).with_for_update()).mappings().one_or_none()
        if row is None:
            raise OperationActionError("OPERATION_NOT_FOUND")
        if row["state"] != "DEAD_LETTERED":
            raise OperationActionError("OPERATION_NOT_DEAD_LETTERED")
        if row["kind"] not in QUEUES:
            raise OperationActionError("OPERATION_NOT_RETRYABLE")
        if row["kind"] == "APPLY_MOCK":
            application = connection.execute(select(m.applications.c.state).where(
                m.applications.c.id == row["entity_id"]).with_for_update()).scalar_one_or_none()
            if application is None:
                raise OperationActionError("APPLICATION_NOT_FOUND")
            if application in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
                raise OperationActionError("RECONCILIATION_REQUIRED")
            if application == "CONFIRMED":
                raise OperationActionError("APPLICATION_ALREADY_CONFIRMED")
            if application not in ("PREPARED", "DRY_RUN_COMPLETE"):
                raise OperationActionError("APPLICATION_REVIEW_REQUIRED")
        values = {"state": "QUEUED", "cancel_requested": False,
                  "lease_expires_at": None, "updated_at": timestamp}
        changed = connection.execute(update(m.operations).where(
            m.operations.c.id == operation_id,
            m.operations.c.state == "DEAD_LETTERED").values(**values))
        if changed.rowcount != 1:
            raise OperationActionError("OPERATION_NOT_DEAD_LETTERED")
        store._audit(connection, operation_id, "DEAD_LETTER_RETRY_REQUESTED", {
            "attempt_count": int(row["attempt_count"]),
            "reason": row["error"],
        })
    return {"operation_id": operation_id, "state": "QUEUED"}


def discard_dead_letter(store, operation_id):
    """Explicitly discard a dead letter while retaining its row and audit history."""
    timestamp = now()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id).with_for_update()).mappings().one_or_none()
        if row is None:
            raise OperationActionError("OPERATION_NOT_FOUND")
        if row["state"] != "DEAD_LETTERED":
            raise OperationActionError("OPERATION_NOT_DEAD_LETTERED")
        values = {"state": "DISCARDED", "discarded_at": timestamp,
                  "lease_expires_at": None, "updated_at": timestamp}
        changed = connection.execute(update(m.operations).where(
            m.operations.c.id == operation_id,
            m.operations.c.state == "DEAD_LETTERED").values(**values))
        if changed.rowcount != 1:
            raise OperationActionError("OPERATION_NOT_DEAD_LETTERED")
        store._audit(connection, operation_id, "DEAD_LETTER_DISCARDED", {
            "attempt_count": int(row["attempt_count"]),
            "reason": row["error"],
        })
    return {"operation_id": operation_id, "state": "DISCARDED"}


def recover_expired(store):
    timestamp = now()
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        rows = list(connection.execute(select(m.operations).where(m.operations.c.state == "RUNNING", m.operations.c.lease_expires_at <= timestamp).with_for_update(skip_locked=True)).mappings())
        for row in rows:
            if row["kind"] in INLINE_KINDS:
                state, error = "FAILED", "LEASE_EXPIRED"
            else:
                state, error = ("QUEUED", "LEASE_EXPIRED") if row["attempt_count"] < MAX_ATTEMPTS else ("DEAD_LETTERED", "RETRY_LIMIT")
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
            values = {"state": state, "error": error, "lease_expires_at": None,
                      "updated_at": timestamp}
            if state == "DEAD_LETTERED":
                values["dead_lettered_at"] = timestamp
            connection.execute(update(m.operations).where(
                m.operations.c.id == row["id"]).values(**values))
            action = "OPERATION_DEAD_LETTERED" if state == "DEAD_LETTERED" else "LEASE_RECOVERY"
            store._audit(connection, row["id"], action, {
                "state": state, "error": error, "attempt_count": int(row["attempt_count"]),
            })
        return len(rows)
