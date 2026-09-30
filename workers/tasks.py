"""One durable operation claim per job, with sanitized failure persistence."""
import os
from pathlib import Path

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.applications.mock_adapter import apply_mock
from services.discovery.pipeline import discover


def execute_operation(operation_id):
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    try:
        with store.engine.begin() as connection:
            changed = connection.execute(update(m.operations).where(m.operations.c.id == operation_id,
                m.operations.c.state == "QUEUED").values(state="RUNNING", updated_at=now()))
            if changed.rowcount != 1:
                return {"status": "ALREADY_CLAIMED"}
            operation = dict(connection.execute(select(m.operations).where(m.operations.c.id == operation_id)).mappings().one())
        try:
            origin = os.environ["MJOBS_MOCK_ORIGIN"]
            if operation["kind"] == "DISCOVER":
                discover(store, origin, operation["entity_id"])
            elif operation["kind"] == "APPLY_MOCK":
                apply_mock(store, operation["entity_id"], origin, Path(os.environ["MJOBS_OUTPUT_DIR"]),
                           submit=operation["params"].get("submit", False))
            else:
                raise ValueError("Unsupported operation")
            state, error = "COMPLETED", None
        except Exception as failure:
            state, error = "FAILED", type(failure).__name__
        with store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(state=state, error=error, updated_at=now()))
        return {"operation_id": operation_id, "status": state, "error_category": error}
    finally:
        store.engine.dispose()
