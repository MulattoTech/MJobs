"""Real PostgreSQL proof for a recovery sweep's secondary application-row wait.

The fixture statement timeout is only a baseline/cleanup guard. Success requires
a natural lock-timeout error while the independently owned blocker remains held.
The production limit is per lock acquisition, not a whole-sweep deadline.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text

from packages.persistence import models as m
from packages.persistence.store import Store, now
from workers.operations import recover_expired


STATEMENT_GUARD_MS = 5000
PHASE_WATCHDOG_SECONDS = 8
_SESSION_SQL = """
SELECT pg_backend_pid(),
       (SELECT EXTRACT(EPOCH FROM backend_start)::text
        FROM pg_stat_activity WHERE pid = pg_backend_pid()),
       current_setting('application_name'),
       (SELECT setting::integer FROM pg_settings WHERE name = 'lock_timeout'),
       (SELECT setting::integer FROM pg_settings WHERE name = 'statement_timeout')
"""
_SESSION_KEYS = ("backend_pid", "backend_start", "application_name",
                 "lock_timeout_ms", "statement_timeout_ms")


def _session_state(raw, original):
    with raw.cursor() as cursor:
        cursor.execute(_SESSION_SQL)
        state = dict(zip(_SESSION_KEYS, cursor.fetchone()))
    state["same_physical_connection"] = raw is original
    return state


def _recovery_process(url, schema, configured_lock_ms, application_name, channel):
    """Own the actual Store connection; never send credentials or exception SQL."""
    store = None
    phase = None
    original = None
    try:
        store = Store(url)
        store.engine = store.engine.execution_options(schema_translate_map={None: schema})

        def configure(raw, _):
            with raw.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, false), "
                    "set_config('statement_timeout', %s, false), "
                    "set_config('application_name', %s, false)",
                    (str(configured_lock_ms), str(STATEMENT_GUARD_MS), application_name),
                )
            raw.commit()

        def before_application_lock(connection, cursor, statement, parameters, context, many):
            # This dedicated schema has exactly one application. Observe the real
            # production query without changing its SQL or injecting a result.
            if "applications" not in statement or "FOR UPDATE" not in statement:
                return
            state = _session_state(connection.connection.driver_connection, original)
            channel.send({"event": "application_lock", "phase": phase, "session": state})

        event.listen(store.engine, "connect", configure)
        event.listen(store.engine, "before_cursor_execute", before_application_lock)
        with store.engine.begin() as connection:
            original = connection.connection.driver_connection
            initial = _session_state(original, original)
        channel.send({"event": "ready", "session": initial})

        while channel.poll(30):
            phase = channel.recv()
            if phase == "stop":
                break
            if phase not in {"blocked", "retry", "repeat"}:
                raise ValueError("Unknown fixture command")
            started = time.monotonic()
            outcome = {"event": "outcome", "phase": phase}
            try:
                outcome["recovered"] = recover_expired(store)
                outcome["error_type"] = None
                outcome["sqlstate"] = None
            except Exception as error:
                outcome["error_type"] = type(error).__name__
                outcome["sqlstate"] = getattr(getattr(error, "orig", None), "sqlstate", None)
            outcome["elapsed_seconds"] = time.monotonic() - started
            # Retain the natural result before any restoration probe or teardown.
            channel.send(outcome)
            with store.engine.begin() as connection:
                restored = _session_state(connection.connection.driver_connection, original)
            channel.send({"event": "settled", "phase": phase, "session": restored})
    except Exception as error:
        channel.send({"event": "fixture_error", "error_type": type(error).__name__})
    finally:
        if store is not None:
            store.engine.dispose()
        channel.close()


def _seed(engine):
    timestamp = now()
    expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with engine.begin() as connection:
        connection.execute(m.profiles.insert().values(
            id="profile", name="Lock fixture", email="fixture@example.invalid", version="1"))
        connection.execute(m.companies.insert().values(
            id="company", name="Lock fixture", domain="example.invalid"))
        connection.execute(m.jobs.insert().values(
            id="job", company_id="company", requisition="fixture", title="Synthetic job",
            description="No provider access", skills=[], location="Fixture", remote_type="REMOTE"))
        connection.execute(m.routes.insert().values(
            id="route", job_id="job", company_id="company", provider="mock",
            tenant="fixture", requisition="fixture", url="https://example.invalid/apply"))
        connection.execute(m.applications.insert().values(
            id="application", job_id="job", route_id="route", profile_id="profile",
            profile_version="1", state="CONFIRMED", target="MOCK", fact_ids=[], trace=[],
            confirmation={"source": "synthetic fixture"}, created_at=timestamp))
        connection.execute(m.operations.insert().values(
            id="operation", kind="APPLY_MOCK", entity_id="application", params={},
            state="RUNNING", attempt_count=1, retry_count=2, progress=20,
            current_step="APPLYING", updated_at=timestamp, lease_expires_at=expired))


def _database_state(engine, *, lock_operation=False):
    with engine.begin() as connection:
        statement = select(m.operations).where(m.operations.c.id == "operation")
        if lock_operation:
            statement = statement.with_for_update(nowait=True)
        operation = dict(connection.execute(statement).mappings().one())
        application = dict(connection.execute(select(m.applications)).mappings().one())
        audit = [dict(row) for row in connection.execute(select(m.audit)).mappings()]
    return {"operation": operation, "application": application, "audit": audit}


@contextmanager
def _owned_case(configured_lock_ms, record_property):
    """Only the exact owned process and UUID schema are eligible for cleanup."""
    schema = "mjobs_recovery_lock_" + uuid4().hex
    receipt = {"schema": schema, "configured_lock_ms": configured_lock_ms, "phases": {}}
    process = None
    with ExitStack() as stack:
        admin = create_engine(
            os.environ["MJOBS_TEST_POSTGRES_URL"],
            connect_args={"connect_timeout": 5,
                          "options": "-c statement_timeout=5000 -c lock_timeout=1000"},
            pool_timeout=1, hide_parameters=True,
        ).execution_options(schema_translate_map={None: schema})
        stack.callback(admin.dispose)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        def drop_schema():
            # An unjoined helper cannot authorize destructive fixture teardown.
            if process is not None and process.is_alive():
                return
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

        stack.callback(drop_schema)
        m.metadata.create_all(admin, tables=[
            m.profiles, m.companies, m.jobs, m.routes, m.applications, m.operations, m.audit])
        _seed(admin)
        receipt["before"] = _database_state(admin)
        blocker = stack.enter_context(admin.connect())
        transaction = stack.enter_context(blocker.begin())
        blocker_pid = blocker.execute(text("SELECT pg_backend_pid()")).scalar_one()
        blocker.execute(select(m.applications).where(
            m.applications.c.id == "application").with_for_update()).one()
        observer = stack.enter_context(
            admin.connect().execution_options(isolation_level="AUTOCOMMIT"))
        observer_pid = observer.execute(text("SELECT pg_backend_pid()")).scalar_one()
        receipt.update(blocker_pid=blocker_pid, observer_pid=observer_pid)
        context = multiprocessing.get_context("spawn")
        parent_channel, child_channel = context.Pipe()
        stack.callback(parent_channel.close)
        stack.callback(child_channel.close)
        process = context.Process(
            target=_recovery_process,
            args=(os.environ["MJOBS_TEST_POSTGRES_URL"], schema, configured_lock_ms,
                  schema, child_channel),
            daemon=True,
        )

        def stop_helper():
            cleanup = {"forced_actions": [], "blocker_release_error": None}
            try:
                # A failed baseline must release only its own blocker first.
                # A broken blocker connection cannot bypass helper shutdown.
                try:
                    if transaction.is_active:
                        transaction.rollback()
                except Exception as error:
                    cleanup["blocker_release_error"] = type(error).__name__
                if process.pid is None:
                    return
                if process.is_alive():
                    try:
                        parent_channel.send("stop")
                    except (BrokenPipeError, EOFError, OSError):
                        pass
                process.join(3)
                if process.is_alive():
                    cleanup["forced_actions"].append("terminate_owned_helper")
                    process.terminate()
                    process.join(3)
                if process.is_alive():
                    cleanup["forced_actions"].append("kill_owned_helper")
                    process.kill()
                    process.join(3)
                cleanup.update(exited=not process.is_alive(), exitcode=process.exitcode)
                assert cleanup["exited"], "Owned helper did not exit; private schema retained"
                assert not cleanup["forced_actions"], "Fixture required forced helper cleanup"
                assert process.exitcode == 0, "Owned helper failed to exit cleanly"
                assert cleanup["blocker_release_error"] is None, "Owned blocker release failed"
            finally:
                record_property("pg_recovery_lock_cleanup", json.dumps(cleanup, sort_keys=True))

        stack.callback(stop_helper)
        process.start()
        child_channel.close()
        try:
            assert parent_channel.poll(15), "Fixture admission: helper did not become ready"
            ready = parent_channel.recv()
            receipt["ready"] = ready
            assert ready["event"] == "ready", "Fixture admission: helper setup failed"
            initial = ready["session"]
            assert initial["backend_pid"] not in {blocker_pid, observer_pid}
            yield admin, blocker, transaction, observer, parent_channel, receipt
        finally:
            # Capture the first outcome before release, process stop or schema drop.
            record_property("pg_recovery_lock_receipt", json.dumps(receipt, sort_keys=True))


def _phase(channel, observer, receipt, name):
    phase = receipt["phases"][name] = {"messages": [], "blocked_witness": None}
    channel.send(name)
    deadline = time.monotonic() + PHASE_WATCHDOG_SECONDS
    target = None
    outcome = None
    while time.monotonic() < deadline:
        if channel.poll(0.01):
            message = channel.recv()
            phase["messages"].append(message)
            assert message.get("phase") == name, "Fixture helper failed or changed phase"
            if message["event"] == "application_lock":
                target = phase["lock_session"] = message["session"]
            elif message["event"] == "outcome":
                outcome = phase["outcome"] = message
            elif message["event"] == "settled":
                phase["restored_session"] = message["session"]
                assert outcome is not None, "Missing natural recovery outcome"
                return phase
        if name == "blocked" and target and not outcome and not phase["blocked_witness"]:
            row = observer.execute(text(
                "SELECT pid, state, wait_event_type, pg_blocking_pids(pid) AS blockers "
                "FROM pg_stat_activity WHERE pid = :pid "
                "AND EXTRACT(EPOCH FROM backend_start)::text = :started "
                "AND application_name = :name"
            ), {"pid": target["backend_pid"], "started": target["backend_start"],
                "name": target["application_name"]}).mappings().one_or_none()
            if (row and row["wait_event_type"] == "Lock"
                    and receipt["blocker_pid"] in row["blockers"]):
                phase["blocked_witness"] = dict(row)
    raise AssertionError("Fixture observation watchdog expired; retained outcome is not PASS")


def _assert_restored(session, initial, configured_lock_ms):
    assert session["same_physical_connection"] is True
    assert session["backend_pid"] == initial["backend_pid"]
    assert session["backend_start"] == initial["backend_start"]
    assert session["application_name"] == initial["application_name"]
    assert session["lock_timeout_ms"] == configured_lock_ms
    assert session["statement_timeout_ms"] == STATEMENT_GUARD_MS


@pytest.mark.skipif(not os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                    reason="Disposable PostgreSQL URL required")
@pytest.mark.parametrize("configured_lock_ms", [0, 500], ids=["unlimited", "stricter"])
def test_recovery_bounds_secondary_application_lock(configured_lock_ms, record_property):
    with _owned_case(configured_lock_ms, record_property) as case:
        admin, blocker, transaction, observer, channel, receipt = case
        initial = receipt["ready"]["session"]
        _assert_restored(initial, initial, configured_lock_ms)
        phase = _phase(channel, observer, receipt, "blocked")
        assert phase["blocked_witness"], "Fixture admission: real owned lock wait not observed"
        assert phase["blocked_witness"]["blockers"] == [receipt["blocker_pid"]]
        lock_session = phase["lock_session"]
        assert lock_session["same_physical_connection"] is True
        assert lock_session["backend_pid"] == initial["backend_pid"]
        assert lock_session["lock_timeout_ms"] == min(configured_lock_ms or 1000, 1000)
        assert lock_session["statement_timeout_ms"] == STATEMENT_GUARD_MS
        assert phase["outcome"]["sqlstate"] == "55P03", "Expected natural PostgreSQL lock timeout"
        assert phase["outcome"]["elapsed_seconds"] < 4, "Fixture statement guard is not the oracle"
        assert transaction.is_active
        assert blocker.execute(text("SELECT pg_backend_pid()")).scalar_one() == receipt["blocker_pid"]
        receipt["after_timeout"] = _database_state(admin, lock_operation=True)
        assert receipt["after_timeout"] == receipt["before"], "Failed recovery changed durable truth"
        _assert_restored(phase["restored_session"], initial, configured_lock_ms)

        transaction.rollback()
        retry = _phase(channel, observer, receipt, "retry")
        assert retry["outcome"]["error_type"] is None
        assert retry["outcome"]["recovered"] == 1
        _assert_restored(retry["restored_session"], initial, configured_lock_ms)
        receipt["after_retry"] = _database_state(admin, lock_operation=True)
        operation = receipt["after_retry"]["operation"]
        assert operation["state"] == "COMPLETED" and operation["error"] is None
        assert operation["lease_expires_at"] is None
        assert operation["attempt_count"] == 1 and operation["retry_count"] == 2
        assert operation["progress"] == 100 and operation["completed_at"]
        assert receipt["after_retry"]["application"] == receipt["before"]["application"]
        assert len(receipt["after_retry"]["audit"]) == 1
        audit = receipt["after_retry"]["audit"][0]
        assert audit["entity_id"] == "operation" and audit["action"] == "LEASE_RECOVERY"
        assert audit["payload"] == {"state": "COMPLETED", "error": None, "attempt_count": 1}

        repeated = _phase(channel, observer, receipt, "repeat")
        assert repeated["outcome"]["error_type"] is None
        assert repeated["outcome"]["recovered"] == 0
        _assert_restored(repeated["restored_session"], initial, configured_lock_ms)
        receipt["after_repeat"] = _database_state(admin, lock_operation=True)
        assert receipt["after_repeat"] == receipt["after_retry"], "Repeated sweep duplicated recovery"
