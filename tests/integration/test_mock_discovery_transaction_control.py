"""Fixture discovery must not commit a batch after its control callback stops it."""
from copy import deepcopy
from shutil import copyfile

import pytest
from sqlalchemy import select

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery import pipeline
from tests.integration.test_spine import PROFILE
from workers.operations import OperationCancelled, OperationTimedOut


# These records share the discovery job transaction. Company/source enrollment
# precedes that transaction and is intentionally outside this rollback contract.
_BATCH_TABLES = (
    m.jobs, m.routes, m.postings, m.matches, m.job_description_revisions,
    m.job_lifecycle_observations,
)
_BATCH_AUDIT_ACTIONS = ("JOB_DISCOVERED", "MATCH_SCORED", "MOCK_DISCOVERY_COMPLETE")



@pytest.fixture(scope="module")
def database_template(tmp_path_factory):
    """Create the schema once; each test gets its own closed-file copy."""
    path = tmp_path_factory.mktemp("discovery-control-schema") / "template.db"
    store = Store(f"sqlite:///{path.as_posix()}")
    try:
        store.migrate()
        store.bind_workspace_mode("FIXTURE")
        store.save_profile(deepcopy(PROFILE))
    finally:
        store.engine.dispose()
    return path


def _new_store(tmp_path, database_template):
    path = tmp_path / "batch.db"
    copyfile(database_template, path)
    return Store(f"sqlite:///{path.as_posix()}")


def _snapshot(store):
    with store.engine.connect() as connection:
        records = {
            table.name: [dict(row) for row in connection.execute(
                select(table).order_by(table.c.id)).mappings()]
            for table in _BATCH_TABLES
        }
        records["batch_audit"] = [dict(row) for row in connection.execute(
            select(m.audit).where(m.audit.c.action.in_(_BATCH_AUDIT_ACTIONS))
            .order_by(m.audit.c.id)).mappings()]
    return records


@pytest.mark.parametrize("terminal_type", [OperationCancelled, OperationTimedOut])
@pytest.mark.parametrize("capture_only", [False, True])
@pytest.mark.parametrize("stop_at", ["first_posting", "completion_audit"])
def test_stopped_batch_rolls_back_and_can_be_retried(
        tmp_path, monkeypatch, database_template, terminal_type, capture_only, stop_at):
    store = _new_store(tmp_path, database_template)
    url = store.url
    terminal = terminal_type("SYNTHETIC_DISCOVERY_STOP")
    stopped = False
    writes = []
    closed = []
    real_persist = pipeline.persist_posting
    real_audit = store._audit
    real_close = pipeline.MockClient.close

    def check():
        if stopped:
            raise terminal

    def persist(connection, posting):
        nonlocal stopped
        result = real_persist(connection, posting)
        writes.append(posting["id"])
        if stop_at == "first_posting":
            stopped = True
        return result

    def audit(connection, entity_id, action, payload):
        nonlocal stopped
        result = real_audit(connection, entity_id, action, payload)
        if action == "MOCK_DISCOVERY_COMPLETE" and stop_at == "completion_audit":
            stopped = True
        return result

    def close(client):
        try:
            return real_close(client)
        finally:
            closed.append(True)

    try:
        before = _snapshot(store)
        with fixture_server(tmp_path / "receipts.json") as origin:
            with monkeypatch.context() as scoped:
                scoped.setattr(pipeline, "persist_posting", persist)
                scoped.setattr(store, "_audit", audit)
                scoped.setattr(pipeline.MockClient, "close", close)
                with pytest.raises(terminal_type) as raised:
                    pipeline.discover(
                        store, origin, "fixture", check=check, capture_only=capture_only)
                assert raised.value is terminal
            assert closed == [True]
            assert len(writes) == (1 if stop_at == "first_posting" else 4)
            assert _snapshot(store) == before
            reopened = Store(url)
            try:
                assert _snapshot(reopened) == before
                result = pipeline.discover(
                    reopened, origin, "fixture", capture_only=capture_only)
                assert reopened.count("canonical_jobs") == 2
                assert reopened.count("job_postings") == 4
                assert reopened.count("job_matches") == (0 if capture_only else 2)
                if capture_only:
                    assert result["postings_captured"] == 4
                    assert len(result["job_ids"]) == 2
                else:
                    assert {row["requisition"] for row in result} == {"A", "B"}
                with reopened.engine.connect() as connection:
                    completed = connection.execute(select(m.audit.c.id).where(
                        m.audit.c.action == "MOCK_DISCOVERY_COMPLETE")).all()
                assert len(completed) == 1
            finally:
                reopened.engine.dispose()
    finally:
        store.engine.dispose()


def test_cancelled_refresh_preserves_existing_job_history(tmp_path, monkeypatch, database_template):
    store = _new_store(tmp_path, database_template)
    url = store.url
    stopped = False
    terminal = OperationCancelled("SYNTHETIC_DISCOVERY_STOP")
    real_persist = pipeline.persist_posting

    def check():
        if stopped:
            raise terminal

    def persist(connection, posting):
        nonlocal stopped
        result = real_persist(connection, posting)
        stopped = True
        return result

    try:
        with fixture_server(tmp_path / "receipts.json") as origin:
            pipeline.discover(store, origin, "fixture")
            before = _snapshot(store)
            with monkeypatch.context() as scoped:
                scoped.setattr(pipeline, "persist_posting", persist)
                with pytest.raises(OperationCancelled) as raised:
                    pipeline.discover(store, origin, "fixture", check=check)
                assert raised.value is terminal
            reopened = Store(url)
            try:
                assert _snapshot(reopened) == before
            finally:
                reopened.engine.dispose()
    finally:
        store.engine.dispose()


def test_public_worker_timeout_does_not_leave_committed_batch(tmp_path, monkeypatch, database_template):
    """The public operation fails durably and exposes no jobs from its timed-out batch."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from workers import tasks

    store = _new_store(tmp_path, database_template)
    url = store.url
    clock = [100.0]
    writes = []
    real_persist = pipeline.persist_posting

    def persist(connection, posting):
        result = real_persist(connection, posting)
        writes.append(posting["id"])
        # A deterministic elapsed-time boundary after a real transactional
        # write; do not patch the process-wide clock or sleep through a timeout.
        clock[0] = 131.0
        return result

    monkeypatch.setattr(tasks, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(tasks, "timeout", lambda: 30)
    monkeypatch.setattr(pipeline, "persist_posting", persist)
    monkeypatch.setenv("MJOBS_DATABASE_URL", url)
    monkeypatch.setenv("MJOBS_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("MJOBS_OPERATION_MODE", "FIXTURE")
    try:
        before = _snapshot(store)
        with fixture_server(tmp_path / "receipts.json") as origin:
            monkeypatch.setenv("MJOBS_MOCK_ORIGIN", origin)
            with TestClient(create_app(
                    store, origin, tmp_path, "redis://127.0.0.1:1/0",
                    operation_mode="FIXTURE")) as api:
                admitted = api.post("/discovery", json={
                    "profile_id": "fixture", "request_id": "bounded-pipeline-batch"})
                assert admitted.status_code == 202, admitted.text
                operation_id = admitted.json()["operation_id"]
                result = tasks.execute_operation(operation_id)
                assert result["status"] == "DEAD_LETTERED"
                assert result["error_category"] == "OPERATION_TIMEOUT"
                saved = api.get(f"/operations/{operation_id}").json()
                assert (saved["state"], saved["error"], saved["attempt_count"]) == (
                    "DEAD_LETTERED", "OPERATION_TIMEOUT", 1)
            reopened = Store(url)
            try:
                with TestClient(create_app(
                        reopened, origin, tmp_path, "redis://127.0.0.1:1/0",
                        operation_mode="FIXTURE")) as api:
                    durable = api.get(f"/operations/{operation_id}").json()
                    assert (durable["state"], durable["error"]) == (
                        "DEAD_LETTERED", "OPERATION_TIMEOUT")
                assert _snapshot(reopened) == before
                assert len(writes) == 1
            finally:
                reopened.engine.dispose()
    finally:
        store.engine.dispose()
