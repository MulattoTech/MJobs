"""Private durable closure evidence; recording never releases browser capacity.

Only the enrolled private observer may contribute a fresh native observation.
This API is internal: it accepts an observer, never caller-supplied wire proof.
Creator-independent persistence and explicit recovery remain separate steps.
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
    with store.engine.connect() as connection:
        row = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id)).mappings().one_or_none()
        scope = _scope(row, owner_token)
        if scope is None:
            return False
        baseline = deepcopy(row["recovery_metadata"])
    if (baseline["manual_auth_owner"].get("tree_observer") != identity
            or not _same_host(baseline["manual_auth_owner"], identity)):
        return False
    try:
        reply = observer.observe(scope)
        if (not isinstance(reply, dict) or set(reply) != {"binding", "status", "accounting"}
                or reply["binding"] != scope or reply["status"] != "EXITED"
                or _status(reply["accounting"]) != "EXITED"
                or reply["accounting"]["terminated"] != 0 or observer.identity != identity):
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
