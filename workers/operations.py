"""Bounded claims and safe recovery; application state owns submission truth."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import os
from uuid import uuid4

from sqlalchemy import func, or_, select, text, update

from packages.persistence import models as m
from packages.persistence.store import now, identity
from services.audit_history import record_application_preparation_event

QUEUES = {"DISCOVER": "mjobs-discovery", "COMPANY_DISCOVER": "mjobs-company-discovery",
          "AI_EVALUATE_QUEUED": "mjobs-ai",
          "COMPANY_SOURCE_REFRESH": "mjobs-company-discovery", "PREPARE": "mjobs-document",
          "RESUME_EXTRACT_QUEUED": "mjobs-document", "UPLOADED_RESUME_COMPARE_QUEUED": "mjobs-document",
          "APPLY_MOCK": "mjobs-application", "APPLICATION_FORM_REVIEW": "mjobs-application",
          "APPLICATION_DRY_RUN": "mjobs-application"}
OPERATION_CATEGORIES = {
    "DISCOVER": "source_collection",
    "COMPANY_DISCOVER": "company_discovery",
    "COMPANY_SOURCE_REFRESH": "company_discovery",
    "AI_EVALUATE": "ai",
    "AI_EVALUATE_QUEUED": "ai",
    "RESUME_EXTRACT": "document_generation",
    "RESUME_EXTRACT_QUEUED": "document_generation",
    "UPLOADED_RESUME_COMPARE": "document_generation",
    "UPLOADED_RESUME_COMPARE_QUEUED": "document_generation",
    "PREPARE": "document_generation",
    "APPLY_MOCK": "application_automation",
    "APPLICATION_FORM_REVIEW": "application_automation",
    "APPLICATION_DRY_RUN": "application_automation",
    "MANUAL_AUTH": "application_automation",
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
INLINE_KINDS = ("AI_EVALUATE", "RESUME_EXTRACT", "UPLOADED_RESUME_COMPARE", "MANUAL_AUTH")


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


def _expire_inline(connection, timestamp):
    # Inline work is never replayed after the owning API process exits.
    connection.execute(update(m.operations).where(
        m.operations.c.kind.in_(tuple(kind for kind in INLINE_KINDS if kind != "MANUAL_AUTH")),
        m.operations.c.state == "RUNNING",
        m.operations.c.lease_expires_at <= timestamp,
    ).values(state="FAILED", error="LEASE_EXPIRED", lease_expires_at=None,
             updated_at=timestamp, completed_at=timestamp, current_step="FINALIZING"))
    # A missed lease is not proof that a headed browser has exited. Retain
    # its capacity until the owning call confirms closure; never replay it.
    connection.execute(update(m.operations).where(
        m.operations.c.kind == "MANUAL_AUTH", m.operations.c.state == "RUNNING",
        m.operations.c.lease_expires_at <= timestamp,
    ).values(state="BLOCKED", error="BROWSER_RELEASE_UNCONFIRMED",
             updated_at=timestamp, current_step="FINALIZING"))


def _browser_capacity_held():
    return ((m.operations.c.state == "RUNNING") |
            ((m.operations.c.kind == "MANUAL_AUTH") &
             (m.operations.c.state == "BLOCKED") &
             (m.operations.c.error == "BROWSER_RELEASE_UNCONFIRMED")))


def _application_browser_busy(connection, category, kind, entity_id):
    if category != "application_automation":
        return False
    # A saved browser profile cannot be opened by a worker and a manual
    # window simultaneously, even when the global ceiling is greater than one.
    return bool(connection.execute(select(func.count()).select_from(m.operations).where(
        m.operations.c.kind.in_(CATEGORY_KINDS[category]),
        _browser_capacity_held(),
        m.operations.c.entity_id == entity_id,
        (m.operations.c.kind == "MANUAL_AUTH") if kind != "MANUAL_AUTH" else True,
    )).scalar_one())


def claim(store, operation_id):
    with store.engine.begin() as connection:
        kind = connection.execute(select(m.operations.c.kind).where(m.operations.c.id == operation_id)).scalar_one()
        category = OPERATION_CATEGORIES[kind]
        _lock(connection, category)
        _expire_inline(connection, now())
        row = connection.execute(select(m.operations).where(m.operations.c.id == operation_id).with_for_update()).mappings().one()
        if (row["state"] != "QUEUED" or row["cancel_requested"]
                or (row["next_attempt_at"] and row["next_attempt_at"] > now())):
            return None
        active = connection.execute(select(func.count()).select_from(m.operations).where(
            m.operations.c.kind.in_(CATEGORY_KINDS[category]), _browser_capacity_held())).scalar_one()
        if active >= limit(kind) or _application_browser_busy(connection, category, kind, row["entity_id"]):
            return None
        timestamp = now()
        if kind == "PREPARE":
            from services.matching.preparation_priority import scheduling_key
            candidates = connection.execute(select(m.operations).where(
                m.operations.c.kind == "PREPARE", m.operations.c.state == "QUEUED",
                m.operations.c.cancel_requested.is_(False),
                or_(m.operations.c.next_attempt_at.is_(None), m.operations.c.next_attempt_at <= timestamp),
            )).mappings().all()
            # A stale FIFO delivery does not consume an attempt. The outbox
            # redispatches it after the RQ delivery finishes, preserving its ID.
            if min(candidates, key=lambda candidate: scheduling_key(candidate, timestamp))["id"] != operation_id:
                return None
        values = {"state": "RUNNING", "attempt_count": row["attempt_count"] + 1,
                  "last_attempt_at": timestamp, "updated_at": timestamp,
                  "current_step": "PREPARING", "progress": max(1, row["progress"] or 0),
                  "started_at": row["started_at"] or timestamp, "completed_at": None,
                  "next_attempt_at": None,
                  "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=timeout() + 30)).isoformat()}
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(**values))
        store._audit(connection, operation_id, "OPERATION_ATTEMPT_STARTED",
                     {"attempt_count": values["attempt_count"]})
        if kind == "PREPARE" and isinstance(row["params"].get("job_id"), str):
            record_application_preparation_event(
                connection, "APPLICATION_STARTED", row["entity_id"],
                row["params"]["job_id"], operation_id, values["attempt_count"])
        return {**dict(row), **values}


def admit_inline(store, kind, entity_id, *, owner_token=None, manual_auth_identity=None):
    """Reserve a bounded operation that executes in the API process, not RQ."""
    if kind not in INLINE_KINDS or kind in QUEUES:
        raise ValueError("Unsupported inline operation kind")
    recovery_metadata = {}
    if owner_token is not None or manual_auth_identity is not None:
        if (kind != "MANUAL_AUTH" or not isinstance(owner_token, str)
                or len(owner_token) != 64 or not isinstance(manual_auth_identity, dict)
                or set(manual_auth_identity) != {"profile_id", "attempt_id"}
                or any(not isinstance(value, str) or not value.strip()
                       for value in manual_auth_identity.values())):
            raise ValueError("Invalid manual browser ownership")
        recovery_metadata["manual_auth_owner"] = {
            **manual_auth_identity, "token_sha256": sha256(owner_token.encode()).hexdigest(),
        }
        from services.applications.browser_process import capture_owner_identity

        recovery_metadata["manual_auth_owner"].update(
            api_process=capture_owner_identity(), api_process_identified=True)
    category = OPERATION_CATEGORIES[kind]
    timestamp = now()
    operation_id = uuid4().hex
    with store.engine.begin() as connection:
        _lock(connection, category)
        # Inline operations are never retried. Manual browser capacity requires
        # confirmed closure; other expired inline operations free capacity.
        _expire_inline(connection, timestamp)
        active = connection.execute(select(func.count()).select_from(m.operations).where(
            m.operations.c.kind.in_(CATEGORY_KINDS[category]),
            _browser_capacity_held())).scalar_one()
        if active >= category_limit(category) or _application_browser_busy(connection, category, kind, entity_id):
            raise OperationConcurrencyLimit(category)
        connection.execute(m.operations.insert().values(
            id=operation_id, kind=kind, entity_id=entity_id, params={},
            state="RUNNING", error=None, updated_at=timestamp, attempt_count=1,
            current_step="PREPARING", progress=1, started_at=timestamp,
            completed_at=None, retry_count=0, recovery_metadata=recovery_metadata,
            cancel_requested=False,
            lease_expires_at=(datetime.now(timezone.utc)
                              + timedelta(seconds=timeout() + 30)).isoformat(),
        ))
    return operation_id


def record_manual_auth_process(store, operation_id, identity, *, owner_token):
    """Persist one immutable root identity from the admitted browser owner's call."""
    from services.applications.browser_process import valid_identity

    if not valid_identity(identity) or not isinstance(owner_token, str) or len(owner_token) != 64:
        return False
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id, m.operations.c.kind == "MANUAL_AUTH",
            m.operations.c.state == "RUNNING").with_for_update()).mappings().one_or_none()
        if row is None:
            return False
        metadata = row["recovery_metadata"] if isinstance(row["recovery_metadata"], dict) else {}
        owner = metadata.get("manual_auth_owner")
        digest = owner.get("token_sha256") if isinstance(owner, dict) else None
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or any(not isinstance(owner.get(key), str) or not owner[key].strip()
                       for key in ("profile_id", "attempt_id"))
                or not compare_digest(sha256(owner_token.encode()).hexdigest(), digest)):
            return False
        if "process" in owner:
            return owner["process"] == identity and owner.get("process_identified") is True
        if "process_identified" in owner:
            return False
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
            recovery_metadata={**metadata, "manual_auth_owner": {
                **owner, "process": dict(identity), "process_identified": True}},
            updated_at=now()))
        store._audit(connection, operation_id, "MANUAL_AUTH_PROCESS_IDENTIFIED",
                     {"profile_id": owner["profile_id"], "attempt_id": owner["attempt_id"]})
        return True


def finish_inline(store, operation_id, state, error=None, *, browser_closed=False, owner_token=None):
    if state not in ("COMPLETED", "CANCELLED", "FAILED", "BLOCKED"):
        raise ValueError("Invalid inline operation terminal state")
    if state == "BLOCKED" and browser_closed:
        raise ValueError("Confirmed browser closure cannot retain blocked capacity")
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = connection.execute(select(
            m.operations.c.id, m.operations.c.entity_id,
            m.operations.c.state, m.operations.c.cancel_requested, m.operations.c.kind, m.operations.c.error,
            m.operations.c.recovery_metadata,
        ).where(m.operations.c.id == operation_id,
                m.operations.c.kind.in_(INLINE_KINDS)).with_for_update()).one_or_none()
        if row is None or (row.state != "RUNNING" and not (
                row.kind == "MANUAL_AUTH" and row.state == "BLOCKED"
                and row.error == "BROWSER_RELEASE_UNCONFIRMED")):
            return False
        if state == "BLOCKED" and (row.kind != "MANUAL_AUTH" or error != "BROWSER_RELEASE_UNCONFIRMED"):
            raise ValueError("Only uncertain manual browser release can retain inline capacity")
        if row.kind == "MANUAL_AUTH" and browser_closed:
            # A closure flag is meaningful only from the admitted call. Unknown
            # legacy owners remain held; this is not crashed-browser exit proof.
            metadata = row.recovery_metadata if isinstance(row.recovery_metadata, dict) else {}
            owner = metadata.get("manual_auth_owner")
            digest = owner.get("token_sha256") if isinstance(owner, dict) else None
            if (not isinstance(owner_token, str) or len(owner_token) != 64
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or any(not isinstance(owner.get(key), str) or not owner[key].strip()
                           for key in ("profile_id", "attempt_id"))
                    or not compare_digest(sha256(owner_token.encode()).hexdigest(), digest)):
                return False
            if any(key in owner for key in ('tree_observer', 'tree_retention', 'tree_closure')):
                from services.applications.manual_auth_recovery import confirmed_tree_closure

                if not confirmed_tree_closure(row._mapping):
                    connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                        state="BLOCKED", error="BROWSER_RELEASE_UNCONFIRMED", updated_at=now(),
                        current_step="FINALIZING", completed_at=None, lease_expires_at=None))
                    return False
            if "process" in owner or "process_identified" in owner:
                from services.applications.browser_process import process_status

                if owner.get("process_identified") is not True or process_status(owner.get("process")) != "EXITED":
                    connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                        state="BLOCKED", error="BROWSER_RELEASE_UNCONFIRMED", updated_at=now(),
                        current_step="FINALIZING", completed_at=None, lease_expires_at=None))
                    return False
        if row.kind == "MANUAL_AUTH" and not browser_closed:
            state, error = "BLOCKED", "BROWSER_RELEASE_UNCONFIRMED"
        if row.cancel_requested and state == "COMPLETED":
            state, error = "CANCELLED", "CANCEL_REQUESTED"
        finished_at = now()
        completed = state == "COMPLETED"
        progress_values = {"progress": 100} if completed else {}
        connection.execute(update(m.operations).where(
            m.operations.c.id == operation_id,
            m.operations.c.kind.in_(INLINE_KINDS),
            m.operations.c.state == row.state,
        ).values(state=state, error=error, updated_at=finished_at,
                 current_step="COMPLETED" if completed else "FINALIZING",
                 completed_at=finished_at, lease_expires_at=None, **progress_values))
        if row.kind == "MANUAL_AUTH" and browser_closed:
            store._audit(connection, operation_id, "MANUAL_AUTH_OWNER_RELEASED",
                         {"profile_id": owner["profile_id"], "attempt_id": owner["attempt_id"],
                          "state": state})
        return True


def inline_cancel_requested(store, operation_id):
    with store.engine.connect() as connection:
        row = connection.execute(select(
            m.operations.c.cancel_requested, m.operations.c.state,
        ).where(m.operations.c.id == operation_id,
                m.operations.c.kind.in_(INLINE_KINDS))).one_or_none()
    return row is None or row.cancel_requested or row.state != "RUNNING"


def check_cancelled(store, operation_id, attempt_count=None):
    """Stop a worker whose durable authority ended, even before recovery runs."""
    with store.engine.connect() as connection:
        row = connection.execute(select(
            m.operations.c.cancel_requested, m.operations.c.state,
            m.operations.c.attempt_count, m.operations.c.lease_expires_at,
        ).where(m.operations.c.id == operation_id)).one()
    if (row.cancel_requested or row.state != "RUNNING"
            or not row.lease_expires_at or row.lease_expires_at <= now()
            or (attempt_count is not None and row.attempt_count != attempt_count)):
        raise OperationCancelled("Operation no longer authorized to continue")


def company_refresh_saved(connection, operation):
    """Read the import transaction's receipt without reversing its lock order.

    Callers hold the operation lock. The source/monitor pointer can change after
    a later refresh, so only the immutable, exactly bound audit is authoritative.
    """
    if operation['kind'] != 'COMPANY_SOURCE_REFRESH':
        return False
    return connection.execute(select(m.audit.c.id).where(
        m.audit.c.action == 'COMPANY_SOURCE_REFRESHED',
        m.audit.c.entity_id == operation['entity_id'],
        m.audit.c.payload['company_source_id'].as_string() == operation['entity_id'],
        m.audit.c.payload['operation_id'].as_string() == operation['id']).limit(1)).first() is not None


def request_cancel(store, operation_id):
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        row = dict(connection.execute(select(m.operations).where(m.operations.c.id == operation_id).with_for_update()).mappings().one())
        if row['state'] in {'QUEUED', 'RUNNING'} and company_refresh_saved(connection, row):
            return {**row, 'reason': 'REFRESH_ALREADY_SAVED'}
        if row['kind'] == 'APPLICATION_DRY_RUN' and row['state'] in {'QUEUED', 'RUNNING'}:
            receipt = connection.execute(select(m.audit.c.id).where(
                m.audit.c.id == identity('APPLICATION_DRY_RUN', operation_id),
                m.audit.c.action == 'APPLICATION_DRY_RUN',
                m.audit.c.entity_id == row['entity_id'])).first()
            if receipt is not None:
                return {**row, 'reason': 'VERIFICATION_ALREADY_SAVED'}
        if row["kind"] == "APPLY_MOCK":
            application = connection.execute(select(m.applications).where(m.applications.c.id == row["entity_id"]).with_for_update()).mappings().one()
            if application["state"] in ("SUBMITTING", "SUBMISSION_UNCONFIRMED", "CONFIRMED"):
                return {**row, "reason": "RECONCILIATION_REQUIRED"}
        if row["state"] not in ("QUEUED", "RUNNING"):
            return {**row, "reason": "TERMINAL_OPERATION"}
        timestamp = now()
        queued_cancel = row["state"] == "QUEUED"
        values = {"cancel_requested": True,
                  "state": "CANCELLED" if queued_cancel else row["state"],
                  "updated_at": timestamp}
        if queued_cancel:
            values.update(completed_at=timestamp, current_step="FINALIZING",
                          next_attempt_at=None,
                          recovery_metadata={"action": "NONE"})
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
            from services.applications.run_requests import application_retry_blocker
            blocker = application_retry_blocker(connection, row)
            if blocker:
                raise OperationActionError(blocker)
        values = {"state": "QUEUED", "cancel_requested": False,
                  "next_attempt_at": None,
                  "lease_expires_at": None, "updated_at": timestamp,
                  "current_step": "RETRYING", "completed_at": None,
                  "retry_count": row["retry_count"] + 1,
                  "recovery_metadata": {"action": "RETRY"}}
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
                  "lease_expires_at": None, "updated_at": timestamp,
                  "completed_at": timestamp, "current_step": "FINALIZING",
                  "recovery_metadata": {"action": "MANUAL_REVIEW"}}
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
        # Queued workers without a lease have no execution authority. Inline
        # browser ownership retains its separate proof-of-closure policy.
        rows = list(connection.execute(select(m.operations).where(
            m.operations.c.state == "RUNNING",
            or_(m.operations.c.lease_expires_at <= timestamp,
                (m.operations.c.kind.in_(tuple(QUEUES)) &
                 m.operations.c.lease_expires_at.is_(None))),
        ).with_for_update(skip_locked=True)).mappings())
        for row in rows:
            if row["kind"] == "MANUAL_AUTH":
                # Cleanup by the original owning call is the only automatic
                # release. A crashed owner requires separately proven cleanup.
                connection.execute(update(m.operations).where(m.operations.c.id == row["id"]).values(
                    state="BLOCKED", error="BROWSER_RELEASE_UNCONFIRMED",
                    updated_at=timestamp, current_step="FINALIZING"))
                continue
            if row["kind"] in INLINE_KINDS:
                state, error = "FAILED", "LEASE_EXPIRED"
            else:
                state, error = ("QUEUED", "LEASE_EXPIRED") if row["attempt_count"] < MAX_ATTEMPTS else ("DEAD_LETTERED", "RETRY_LIMIT")
            if row["cancel_requested"]:
                state, error = "CANCELLED", "CANCEL_REQUESTED"
            elif row['kind'] == 'AI_EVALUATE_QUEUED':
                states = connection.execute(select(m.ai_evaluation_batches.c.candidate_states).where(
                    m.ai_evaluation_batches.c.id == row['id'])).scalar_one()
                if any(value.get('state') == 'CALL_STARTED' for value in states.values()):
                    state, error = 'BLOCKED', 'AI_PROVIDER_OUTCOME_UNKNOWN'
            if row["kind"] == "APPLY_MOCK":
                application = connection.execute(select(m.applications).where(m.applications.c.id == row["entity_id"]).with_for_update()).mappings().one()
                if application["state"] == "CONFIRMED":
                    state, error = "COMPLETED", None
                elif application["state"] in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
                    # Retain application state; this retry can only look up a receipt.
                    state, error = ("QUEUED", "RECONCILIATION_REQUIRED") if row["attempt_count"] < MAX_ATTEMPTS else ("BLOCKED", "RECONCILIATION_REQUIRED")
                else:
                    from services.applications.run_requests import application_retry_blocker
                    blocker = application_retry_blocker(connection, row)
                    if blocker:
                        state, error = "BLOCKED", blocker
                        if blocker == "APPLICATION_REVIEW_REQUIRED":
                            error = "PRE_SUBMIT_REVIEW_REQUIRED"
                            connection.execute(update(m.applications).where(
                                m.applications.c.id == application["id"]).values(state="REVIEW_REQUIRED"))
            refresh_saved = company_refresh_saved(connection, row)
            if refresh_saved:
                state, error = "COMPLETED", None
            values = {"state": state, "error": error, "lease_expires_at": None,
                      "updated_at": timestamp,
                      "current_step": "RECOVERING",
                      "retry_count": row["retry_count"] + (1 if state == "QUEUED" else 0),
                      "recovery_metadata": {"action": "RESUME" if state == "QUEUED" else "MANUAL_REVIEW"}}
            if refresh_saved:
                values.update(cancel_requested=False, recovery_metadata={"action": "NONE"})
            if state in {"COMPLETED", "CANCELLED", "FAILED", "BLOCKED", "DEAD_LETTERED"}:
                values["completed_at"] = timestamp
                if state == "COMPLETED":
                    values["current_step"] = "COMPLETED"
                    values["progress"] = 100
            if state == "DEAD_LETTERED":
                values["dead_lettered_at"] = timestamp
            connection.execute(update(m.operations).where(
                m.operations.c.id == row["id"]).values(**values))
            action = "OPERATION_DEAD_LETTERED" if state == "DEAD_LETTERED" else "LEASE_RECOVERY"
            store._audit(connection, row["id"], action, {
                "state": state, "error": error, "attempt_count": int(row["attempt_count"]),
            })
            if (state == "DEAD_LETTERED" and row["kind"] == "PREPARE"
                    and isinstance(row["params"].get("job_id"), str)):
                record_application_preparation_event(
                    connection, "APPLICATION_FAILED", row["entity_id"],
                    row["params"]["job_id"], row["id"])
        return len(rows)
