"""One durable operation claim per job, with sanitized failure persistence."""
import os
from pathlib import Path

from sqlalchemy import update

from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from workers.operations import claim, check_cancelled, OperationCancelled


def execute_operation(operation_id):
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    try:
        operation = claim(store, operation_id)
        if operation is None:
            return {"status": "ALREADY_CLAIMED_OR_CAPACITY"}
        try:
            origin = os.environ["MJOBS_MOCK_ORIGIN"]
            check = lambda: check_cancelled(store, operation_id)
            check()
            state, error = "COMPLETED", None
            if operation["kind"] == "DISCOVER":
                discover(store, origin, operation["entity_id"], check=check, profile_version=operation["params"].get("profile_version"))
            elif operation["kind"] == "PREPARE":
                prepare(store, operation["params"]["job_id"], operation["entity_id"], Path(os.environ["MJOBS_OUTPUT_DIR"]), check=check, profile_version=operation["params"].get("profile_version"))
            elif operation["kind"] == "APPLY_MOCK":
                application = apply_mock(store, operation["entity_id"], origin, Path(os.environ["MJOBS_OUTPUT_DIR"]),
                           submit=operation["params"].get("submit", False), check=check)
                if application["state"] not in ("CONFIRMED", "DRY_RUN_COMPLETE"):
                    state, error = "BLOCKED", application["state"]
            else:
                raise ValueError("Unsupported operation")
        except OperationCancelled:
            state, error = "CANCELLED", "CANCEL_REQUESTED"
        except Exception as failure:
            state, error = "FAILED", type(failure).__name__
        with store.engine.begin() as connection:
            changed = connection.execute(update(m.operations).where(m.operations.c.id == operation_id,
                m.operations.c.state == "RUNNING", m.operations.c.attempt_count == operation["attempt_count"]).values(state=state, error=error, lease_expires_at=None, updated_at=now()))
            if changed.rowcount != 1:
                return {"operation_id": operation_id, "status": "OWNERSHIP_EXPIRED"}
        return {"operation_id": operation_id, "status": state, "error_category": error}
    finally:
        store.engine.dispose()
