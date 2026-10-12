"""Private durable closure evidence; recording never releases browser capacity.

Only the enrolled private observer may contribute a fresh native observation.
This API is internal: it accepts an observer, never caller-supplied wire proof.
Retained native observers may persist after creator death. Explicit recovery
and production observer bootstrap remain separate steps.
"""
from copy import deepcopy
from hashlib import sha256
from hmac import compare_digest

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import now
from services.applications.browser_tree_ipc import BrowserTreeObserver, validate_binding, _status


def _identity(value):
    return (isinstance(value, dict) and set(value) == {"pid", "created", "host"}
            and type(value["pid"]) is int and 0 < value["pid"] <= 0xffffffff
            and isinstance(value["created"], str) and value["created"].isascii()
            and value["created"].isdigit() and 0 < len(value["created"]) <= 32
            and isinstance(value["host"], str) and len(value["host"]) == 64
            and all(v in "0123456789abcdef" for v in value["host"]))


def _scope(row, token):
    if (row is None or row["kind"] != "MANUAL_AUTH" or not (
            row["state"] == "RUNNING" or (row["state"] == "BLOCKED"
            and row["error"] == "BROWSER_RELEASE_UNCONFIRMED"))
            or not isinstance(token, str) or len(token) != 64):
        return None
    metadata = row["recovery_metadata"]
    owner = metadata.get("manual_auth_owner") if isinstance(metadata, dict) else None
    if not isinstance(owner, dict):
        return None
    try:
        scope = validate_binding({"operation_id": row["id"], "application_id": row["entity_id"],
            "attempt_id": owner.get("attempt_id"), "profile_id": owner.get("profile_id"),
            "owner_digest": owner.get("token_sha256")})
    except ValueError:
        return None
    return scope if compare_digest(scope["owner_digest"], sha256(token.encode()).hexdigest()) else None


def _row(connection, operation_id):
    return connection.execute(select(m.operations).where(
        m.operations.c.id == operation_id).with_for_update()).mappings().one_or_none()


def _lock(connection):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _same_host(owner, identity):
    api = owner.get("api_process")
    return (owner.get("api_process_identified") is True and _identity(api)
            and api["host"] == identity["host"])


def enroll_observer(store, operation_id, observer, *, owner_token):
    """Bind one immutable observer identity before relying on its closure proof."""
    if type(observer) is not BrowserTreeObserver or not _identity(observer.identity):
        return False
    identity = deepcopy(observer.identity)
    with store.engine.begin() as connection:
        _lock(connection)
        row = _row(connection, operation_id)
        scope = _scope(row, owner_token)
        if scope is None or scope != observer._binding:
            return False
        metadata = deepcopy(row["recovery_metadata"])
        owner = metadata["manual_auth_owner"]
        if not _same_host(owner, identity):
            return False
        if "tree_retention" in owner:
            return False
        if "tree_observer" in owner:
            return owner["tree_observer"] == identity
        owner["tree_observer"] = identity
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
            recovery_metadata=metadata, updated_at=now()))
        store._audit(connection, operation_id, "MANUAL_AUTH_TREE_OBSERVER_ENROLLED", {})
        return True


def record_closure(store, operation_id, observer, *, owner_token):
    """Observe outside the transaction, then fence and atomically persist evidence.

    Refuse unknown/live trees, termination, stale scope and replaced owners.
    No state, cancellation, lease, application or attempt transition occurs here.
    """
    if type(observer) is not BrowserTreeObserver or not _identity(observer.identity):
        return False
    identity = deepcopy(observer.identity)
    return _record_closure(store, operation_id, identity, observer._binding,
        observer.observe, lambda: observer.identity == identity, owner_token)


def record_retained_closure(store, operation_id, query, binding, *, owner_token):
    """Persist from the enrolled observer's own retained native authority.

    Only an exact native query object is accepted, never a wire observation.
    The observer must be outside the tree, on the admitted host, and enrolled
    under its actual current PID/creation identity. No API liveness is needed.
    Acquisition/transfer of this handle remains a private bootstrap concern.
    """
    from services.applications.browser_process import capture_owner_identity
    from services.applications.browser_tree import BrowserTreeQuery
    if type(query) is not BrowserTreeQuery:
        return False
    try:
        binding = validate_binding(binding)
        identity = capture_owner_identity()
        key = query._retention_id
        if not _identity(identity):
            return False

        def observe(scope):
            if query.contains(identity["pid"]) is not False or query.limit_flags() != 0:
                return None
            accounting = query.accounting()
            return {"binding": scope, "status": _status(accounting), "accounting": accounting}

        return _record_closure(store, operation_id, identity, binding, observe,
            lambda: capture_owner_identity() == identity and query._retention_id == key,
            owner_token, retention_id=key)
    except Exception:
        # Never expose credentials, connection strings or native diagnostics.
        return False


def enroll_retained_observer(store, operation_id, query, binding, *, owner_token):
    """Enroll the original retained query in the observer process itself.

    An existing IPC enrollment cannot be converted to a different query.
    Bootstrap must call this before relying on this authority for tree proof.
    """
    from services.applications.browser_process import capture_owner_identity
    from services.applications.browser_tree import BrowserTreeQuery
    if type(query) is not BrowserTreeQuery:
        return False
    try:
        binding = validate_binding(binding)
        identity = capture_owner_identity()
        key = query._retention_id
        if (not _identity(identity) or not isinstance(key, str) or len(key) != 64
                or query.contains(identity["pid"]) is not False or query.limit_flags() != 0):
            return False
        with store.engine.begin() as connection:
            _lock(connection)
            row = _row(connection, operation_id)
            if _scope(row, owner_token) != binding:
                return False
            metadata = deepcopy(row["recovery_metadata"])
            owner = metadata["manual_auth_owner"]
            if not _same_host(owner, identity):
                return False
            if "tree_observer" in owner or "tree_retention" in owner:
                return owner.get("tree_observer") == identity and owner.get("tree_retention") == key
            owner["tree_observer"] = identity
            owner["tree_retention"] = key
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                recovery_metadata=metadata, updated_at=now()))
            store._audit(connection, operation_id, "MANUAL_AUTH_TREE_OBSERVER_ENROLLED", {})
            return True
    except Exception:
        return False


def rollback_unlaunched_enrollment(store, operation_id, query, binding, *, owner_token):
    """Compensate failed private bootstrap only before any process admission.

    Never remove an admitted tree's authority, evidence, state or capacity.
    Only the enrolled observer's own exact retained query can compensate.
    """
    from services.applications.browser_process import capture_owner_identity
    from services.applications.browser_tree import BrowserTreeQuery
    if type(query) is not BrowserTreeQuery:
        return False
    try:
        binding = validate_binding(binding)
        identity = capture_owner_identity()
        if not _identity(identity):
            return False
        with store.engine.begin() as connection:
            _lock(connection)
            row = _row(connection, operation_id)
            if _scope(row, owner_token) != binding:
                return False
            metadata = deepcopy(row["recovery_metadata"])
            owner = metadata["manual_auth_owner"]
            if (not _same_host(owner, identity) or owner.get("tree_observer") != identity
                    or owner.get("tree_retention") != query._retention_id
                    or "tree_closure" in owner or query.contains(identity["pid"]) is not False
                    or query.limit_flags() != 0
                    or query.accounting() != {"total": 0, "active": 0, "terminated": 0}):
                return False
            del owner["tree_observer"]
            del owner["tree_retention"]
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
                recovery_metadata=metadata, updated_at=now()))
            store._audit(connection, operation_id, "MANUAL_AUTH_TREE_BOOTSTRAP_ROLLED_BACK", {})
            return True
    except Exception:
        return False


def _record_closure(store, operation_id, identity, binding, observe, unchanged, owner_token, *, retention_id=None):
    with store.engine.connect() as connection:
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id)).mappings().one_or_none()
        scope = _scope(row, owner_token)
        if scope is None or scope != binding:
            return False
        baseline = deepcopy(row["recovery_metadata"])
    if (baseline["manual_auth_owner"].get("tree_observer") != identity
            or not _same_host(baseline["manual_auth_owner"], identity)
            or baseline["manual_auth_owner"].get("tree_retention") != retention_id):
        return False
    try:
        reply = observe(scope)
        if (not isinstance(reply, dict) or set(reply) != {"binding", "status", "accounting"}
                or reply["binding"] != scope or reply["status"] != "EXITED"
                or _status(reply["accounting"]) != "EXITED"
                or reply["accounting"]["terminated"] != 0 or not unchanged()):
            return False
        certificate = {"version": 1, "binding": scope, "observer": identity,
                       "accounting": deepcopy(reply["accounting"]), "observed_at": now()}
    except Exception:
        return False
    with store.engine.begin() as connection:
        _lock(connection)
        current = _row(connection, operation_id)
        if (_scope(current, owner_token) != scope or current["recovery_metadata"] != baseline):
            return False
        owner = baseline["manual_auth_owner"]
        if "tree_closure" in owner:
            previous = owner["tree_closure"]
            return isinstance(previous, dict) and all(previous.get(k) == certificate[k]
                for k in ("version", "binding", "observer", "accounting"))
        owner["tree_closure"] = certificate
        connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(
            recovery_metadata=baseline, updated_at=now()))
        store._audit(connection, operation_id, "MANUAL_AUTH_TREE_CLOSURE_OBSERVED", {})
        return True
