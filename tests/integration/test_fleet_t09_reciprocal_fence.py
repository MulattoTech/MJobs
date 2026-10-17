"""Existing physical/manual browser admission shares one durable ownership fence."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
from unittest.mock import patch

import pytest
from sqlalchemy import event, select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict, finish_attempt
from services.documents.generate import prepare
from tests.integration.test_fleet_t09_connected_admission import prepared, reserve
from workers.operations import OperationConcurrencyLimit, admit_inline


def require_manual_review(case):
    attempt = reserve(case, "historical-auth-required")
    case.store.transition(case.app_id, ["PREPARED"], "AUTH_REQUIRED")
    finish_attempt(case.store, attempt["id"], case.store.application(case.app_id))


def admission_lock_statement(statement):
    normalized = statement.strip().upper()
    return normalized == "BEGIN IMMEDIATE" or "PG_ADVISORY_XACT_LOCK(" in normalized


def test_running_physical_attempt_blocks_manual_admission_after_reopen(prepared):
    case = prepared
    require_manual_review(case)
    attempt = reserve(case, "physical-owner")
    with case.store.engine.begin() as connection:
        connection.execute(update(m.attempts).where(m.attempts.c.id == attempt["id"]).values(
            started_at="2000-01-01T00:00:00+00:00"))
    case.store.engine.dispose()
    reopened = Store(case.store.url)
    try:
        with patch.dict("os.environ", {"MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "2"}):
            with pytest.raises(OperationConcurrencyLimit):
                admit_inline(reopened, "MANUAL_AUTH", case.app_id)
        with reopened.engine.connect() as connection:
            assert connection.execute(select(m.attempts.c.state).where(
                m.attempts.c.id == attempt["id"])).scalar_one() == "RUNNING"
        assert reopened.count("worker_operations") == 0
    finally:
        reopened.engine.dispose()


@pytest.mark.parametrize("ordering", ["simultaneous", "physical_locked", "manual_locked"])
def test_concurrent_physical_and_manual_admission_has_exactly_one_owner(prepared, ordering):
    case = prepared
    require_manual_review(case)
    barrier = threading.Barrier(2)
    first_actor = {"physical_locked": "physical", "manual_locked": "manual"}.get(ordering)
    first_locked = threading.Event()
    other_attempting = threading.Event()

    def compete(actor):
        store = Store(case.store.url)
        try:
            # Observe real database lock acquisition to exercise both contention
            # orders without replacing the lock or relying on scheduler timing.
            @event.listens_for(store.engine, "after_cursor_execute")
            def acquired(connection, cursor, statement, parameters, context, executemany):
                if actor == first_actor and admission_lock_statement(statement):
                    first_locked.set()
                    assert other_attempting.wait(timeout=10)

            @event.listens_for(store.engine, "before_cursor_execute")
            def contending(connection, cursor, statement, parameters, context, executemany):
                if (first_actor and actor != first_actor
                        and admission_lock_statement(statement)):
                    other_attempting.set()

            barrier.wait(timeout=10)
            if first_actor and actor != first_actor:
                assert first_locked.wait(timeout=10)
            try:
                if actor == "physical":
                    result = reserve(SimpleNamespace(store=store, app_id=case.app_id),
                                     f"race-physical-{ordering}")
                    return actor, "ADMITTED", result["id"]
                return actor, "ADMITTED", admit_inline(store, "MANUAL_AUTH", case.app_id)
            except AttemptConflict as error:
                return actor, "BLOCKED", error.code
            except OperationConcurrencyLimit:
                return actor, "BLOCKED", "APPLICATION_BROWSER_BUSY"
        finally:
            store.engine.dispose()

    with patch.dict("os.environ", {"MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "2"}):
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(compete, ("physical", "manual")))
        assert sorted(status for _, status, _ in outcomes) == ["ADMITTED", "BLOCKED"], outcomes
        winner = next(actor for actor, status, _ in outcomes if status == "ADMITTED")
        if first_actor:
            assert winner == first_actor, outcomes
        with case.store.engine.connect() as connection:
            attempts = list(connection.execute(select(m.attempts.c.id).where(
                m.attempts.c.application_id == case.app_id, m.attempts.c.state == "RUNNING")))
            windows = list(connection.execute(select(m.operations.c.id).where(
                m.operations.c.entity_id == case.app_id, m.operations.c.kind == "MANUAL_AUTH",
                m.operations.c.state == "RUNNING")))
        assert len(attempts) + len(windows) == 1
        case.store.engine.dispose()
        reopened = Store(case.store.url)
        try:
            if winner == "physical":
                with pytest.raises(OperationConcurrencyLimit):
                    admit_inline(reopened, "MANUAL_AUTH", case.app_id)
            else:
                with pytest.raises(AttemptConflict, match="APPLICATION_RUN_IN_PROGRESS"):
                    reserve(SimpleNamespace(store=reopened, app_id=case.app_id), "after-restart")
        finally:
            reopened.engine.dispose()


def test_different_application_manual_admission_remains_available_at_capacity_two(prepared):
    case = prepared
    reserve(case, "physical-owner")
    job_id = case.store.application(case.app_id)["job_id"]
    other_job = next(row for row in case.store.jobs("fixture") if row["id"] != job_id)
    other_id = prepare(case.store, other_job["id"], "fixture", case.root)
    other = SimpleNamespace(store=case.store, app_id=other_id)
    require_manual_review(other)
    with patch.dict("os.environ", {"MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "2"}):
        assert admit_inline(case.store, "MANUAL_AUTH", other_id)
