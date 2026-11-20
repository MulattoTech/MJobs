"""Real PostgreSQL proof that optional post-commit context has a lock-wait cap.

The saved dry-run receipt is synthetic, not browser or provider acceptance proof.
A real applications table lock blocks only enrichment after recovery commits.
The five-second statement timeout is a fixture guard, never a passing oracle.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import multiprocessing
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text

from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from tests.integration.test_operation_recovery_lock_budget import (
    PHASE_WATCHDOG_SECONDS, STATEMENT_GUARD_MS, _assert_restored, _session_state,
)
from workers.operation_log_context import resolve_operation_log_context
from workers.operations import recover_expired


_PRIVATE_MARKER = "PRIVATE_LOG_CONTEXT_CANARY"


def _application_select(context):
    statement = getattr(getattr(context, "compiled", None), "statement", None)
    return (getattr(statement, "is_select", False)
            and any(table is m.applications for table in statement.get_final_froms()))


def _context_process(url, schema, configured_lock_ms, channel):
    """Own the Store and observe real SQL/errors without replacing either."""
    store = None
    phase = None
    original = None
    sql_errors = []
    messages = []
    listeners = []
    logger = logging.getLogger("mjobs.operations")
    previous_level, previous_propagation = logger.level, logger.propagate

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        store = Store(url)
        store.engine = store.engine.execution_options(schema_translate_map={None: schema})

        def configure(raw, _):
            with raw.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, false), "
                    "set_config('statement_timeout', %s, false), "
                    "set_config('application_name', %s, false)",
                    (str(configured_lock_ms), str(STATEMENT_GUARD_MS), schema),
                )
            raw.commit()

        def before_select(connection, cursor, statement, parameters, context, many):
            if _application_select(context):
                session = _session_state(connection.connection.driver_connection, original)
                channel.send({"event": "application_select", "phase": phase, "session": session})

        def observe_error(context):
            # Retain only classification, not exception text, SQL or credentials.
            error = {"error_type": type(context.original_exception).__name__,
                     "sqlstate": getattr(context.original_exception, "sqlstate", None),
                     "application_select": _application_select(context.execution_context)}
            sql_errors.append(error)
            channel.send({"event": "sql_error", "phase": phase, **error})

        for name, listener in (("connect", configure), ("before_cursor_execute", before_select),
                               ("handle_error", observe_error)):
            event.listen(store.engine, name, listener)
            listeners.append((name, listener))
        with store.engine.begin() as connection:
            original = connection.connection.driver_connection
            initial = _session_state(original, original)
        channel.send({"event": "ready", "session": initial})

        while channel.poll(30):
            phase = channel.recv()
            if phase == "stop":
                break
            if phase not in {"blocked_recovery", "normal_context", "repeat"}:
                raise ValueError("Unknown fixture command")
            sql_errors.clear()
            messages.clear()
            started = time.monotonic()
            outcome = {"event": "outcome", "phase": phase}
            try:
                if phase == "normal_context":
                    outcome["context"] = resolve_operation_log_context(store, {
                        "id": "operation", "kind": "APPLICATION_DRY_RUN",
                        "entity_id": "application",
                    })
                else:
                    outcome["recovered"] = recover_expired(store)
                outcome["error_type"] = None
            except Exception as error:
                outcome["error_type"] = type(error).__name__
            outcome.update(elapsed_seconds=time.monotonic() - started,
                           sql_errors=list(sql_errors), logs=list(messages))
            # This first natural result is sent before any restoration probe.
            channel.send(outcome)
            with store.engine.begin() as connection:
                restored = _session_state(connection.connection.driver_connection, original)
            channel.send({"event": "settled", "phase": phase, "session": restored})
    except Exception as error:
        channel.send({"event": "fixture_error", "error_type": type(error).__name__})
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagation
        if store is not None:
            for name, listener in listeners:
                event.remove(store.engine, name, listener)
            store.engine.dispose()
        channel.close()


def _seed(engine):
    timestamp = now()
    expired = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    params = {"application_id": "application", "profile_id": "profile",
              "request_id": "synthetic-run", "fingerprint": "a" * 64,
              "context": "CONTROLLED_FIXTURE", "template_id": "synthetic-template",
              "template_sha256": "b" * 64, "private_note": _PRIVATE_MARKER}
    saved = {key: params[key] for key in (
        "application_id", "profile_id", "fingerprint", "context", "template_id", "template_sha256")}
    saved.update(operation_id="operation", verification="BROWSER_SELECTED_BYTES",
                 documents=[], pages=[], provider_acceptance_claimed=False,
                 submission_enabled=False)
    with engine.begin() as connection:
        connection.execute(m.profiles.insert().values(
            id="profile", name="Log context fixture", email="fixture@example.invalid", version="1"))
        connection.execute(m.companies.insert().values(
            id="company", name="Log context fixture", domain="example.invalid"))
        connection.execute(m.jobs.insert().values(
            id="job", company_id="company", requisition="fixture", title="Synthetic job",
            description="No provider access", skills=[], location="Fixture", remote_type="REMOTE"))
        connection.execute(m.routes.insert().values(
            id="route", job_id="job", company_id="company", provider="greenhouse",
            tenant="fixture", requisition="fixture", url="https://example.invalid/apply"))
        connection.execute(m.applications.insert().values(
            id="application", job_id="job", route_id="route", profile_id="profile",
            profile_version="1", state="DRY_RUN_COMPLETE", target="MOCK", fact_ids=[],
            trace=[], created_at=timestamp))
        connection.execute(m.operations.insert().values(
            id="operation", kind="APPLICATION_DRY_RUN", entity_id="application", params=params,
            state="RUNNING", attempt_count=3, retry_count=2, progress=20,
            current_step="DRY_RUNNING", updated_at=timestamp, lease_expires_at=expired))
        connection.execute(m.audit.insert().values(
            id=identity("APPLICATION_DRY_RUN", "operation"), entity_id="application",
            action="APPLICATION_DRY_RUN", payload=saved, timestamp=timestamp))


def _recovery_state(connection, *, lock_operation=False):
    # Never read applications here: its table is deliberately locked.
    query = select(m.operations).where(m.operations.c.id == "operation")
    if lock_operation:
        query = query.with_for_update(nowait=True)
    operation = dict(connection.execute(query).mappings().one())
    audit = [dict(row) for row in connection.execute(select(m.audit).order_by(m.audit.c.id)).mappings()]
    return {"operation": operation, "audit": audit}


def _independent_state(engine):
    with engine.begin() as connection:
        return _recovery_state(connection, lock_operation=True)


def _application_state(engine):
    with engine.begin() as connection:
        return dict(connection.execute(select(m.applications)).mappings().one())


@contextmanager
def _owned_case(configured_lock_ms, record_property):
    schema = "mjobs_log_context_" + uuid4().hex
    receipt = {"schema": schema, "configured_lock_ms": configured_lock_ms, "phases": {}}
    process = None
    with ExitStack() as stack:
        try:
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
                if process is not None and process.is_alive():
                    return
                with admin.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

            stack.callback(drop_schema)
            m.metadata.create_all(admin, tables=[
                m.profiles, m.companies, m.jobs, m.routes, m.applications, m.operations,
                m.audit, m.postings])
            _seed(admin)
            receipt["before"] = _independent_state(admin)
            receipt["before_application"] = _application_state(admin)
            blocker = stack.enter_context(admin.connect())
            transaction = stack.enter_context(blocker.begin())
            blocker_pid = blocker.execute(text("SELECT pg_backend_pid()")).scalar_one()
            blocker.execute(text(f'LOCK TABLE "{schema}".applications IN ACCESS EXCLUSIVE MODE'))
            observer = stack.enter_context(
                admin.connect().execution_options(isolation_level="AUTOCOMMIT"))
            observer_pid = observer.execute(text("SELECT pg_backend_pid()")).scalar_one()
            receipt.update(blocker_pid=blocker_pid, observer_pid=observer_pid)
            context = multiprocessing.get_context("spawn")
            parent_channel, child_channel = context.Pipe()
            stack.callback(parent_channel.close)
            stack.callback(child_channel.close)
            process = context.Process(
                target=_context_process,
                args=(os.environ["MJOBS_TEST_POSTGRES_URL"], schema, configured_lock_ms, child_channel),
                daemon=True,
            )

            def stop_helper():
                cleanup = {"forced_actions": [], "blocker_release_error": None}
                try:
                    try:
                        if transaction.is_active:
                            transaction.rollback()
                    except Exception as error:
                        cleanup["blocker_release_error"] = type(error).__name__
                    if process.pid is None:
                        cleanup["exited"] = True
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
                    record_property("pg_log_context_cleanup", json.dumps(cleanup, sort_keys=True))

            # Register cleanup before start; no cached backend PID is ever killed.
            stack.callback(stop_helper)
            process.start()
            child_channel.close()
            assert parent_channel.poll(15), "Fixture admission: helper did not become ready"
            ready = parent_channel.recv()
            receipt["ready"] = ready
            assert ready["event"] == "ready", "Fixture admission: helper setup failed"
            assert ready["session"]["backend_pid"] not in {blocker_pid, observer_pid}
            yield admin, blocker, transaction, observer, parent_channel, receipt
        finally:
            # First outcomes precede blocker release, process stop and schema drop.
            record_property("pg_log_context_receipt", json.dumps(receipt, sort_keys=True))


def _blocked_backend(observer, target, blocker_pid):
    row = observer.execute(text(
        "SELECT pid, state, wait_event_type, pg_blocking_pids(pid) AS blockers "
        "FROM pg_stat_activity WHERE pid = :pid "
        "AND EXTRACT(EPOCH FROM backend_start)::text = :started AND application_name = :name"
    ), {"pid": target["backend_pid"], "started": target["backend_start"],
        "name": target["application_name"]}).mappings().one_or_none()
    if (row and row["state"] == "active" and row["wait_event_type"] == "Lock"
            and row["blockers"] == [blocker_pid]):
        return dict(row)
    return None


def _phase(channel, observer, receipt, name):
    phase = receipt["phases"][name] = {"messages": [], "committed_while_blocked": None}
    channel.send(name)
    deadline = time.monotonic() + PHASE_WATCHDOG_SECONDS
    target = None
    outcome = None
    while time.monotonic() < deadline:
        if channel.poll(0.01):
            message = channel.recv()
            phase["messages"].append(message)
            assert message.get("phase") == name, "Fixture helper failed or changed phase"
            if message["event"] == "application_select":
                target = phase["lock_session"] = message["session"]
            elif message["event"] == "outcome":
                outcome = phase["outcome"] = message
            elif message["event"] == "settled":
                phase["restored_session"] = message["session"]
                assert outcome is not None, "Missing natural outcome"
                return phase
        if (name == "blocked_recovery" and target and not outcome
                and not phase["committed_while_blocked"]):
            before = _blocked_backend(observer, target, receipt["blocker_pid"])
            if before:
                # Only operations/audit are read. Bracket visibility with two
                # exact owned-backend lock witnesses to exclude a post-timeout read.
                committed = _recovery_state(observer)
                after = _blocked_backend(observer, target, receipt["blocker_pid"])
                if after:
                    phase["committed_while_blocked"] = {
                        "before": before, "state": committed, "after": after,
                    }
    raise AssertionError("Fixture observation watchdog expired; retained outcome is not PASS")


def _assert_committed(state, before):
    operation = state["operation"]
    assert operation["state"] == "COMPLETED" and operation["error"] is None
    assert operation["lease_expires_at"] is None and operation["completed_at"]
    assert operation["attempt_count"] == 3 and operation["retry_count"] == 2
    assert operation["progress"] == 100 and operation["current_step"] == "COMPLETED"
    assert operation["recovery_metadata"] == {"action": "NONE"}
    saved = [row for row in state["audit"] if row["action"] == "APPLICATION_DRY_RUN"]
    assert saved == before["audit"], "Original synthetic receipt changed"
    recovery = [row for row in state["audit"] if row["action"] == "LEASE_RECOVERY"]
    assert len(state["audit"]) == 2 and len(recovery) == 1
    assert recovery[0]["entity_id"] == "operation"
    assert recovery[0]["payload"] == {"state": "COMPLETED", "error": None, "attempt_count": 3}


def _assert_local_cap(session, initial, configured_lock_ms):
    # All identity and unchanged statement-limit checks reuse the restoration oracle.
    _assert_restored(session, initial, min(configured_lock_ms or 1000, 1000))


@pytest.mark.skipif(not os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                    reason="Disposable PostgreSQL URL required")
@pytest.mark.parametrize("configured_lock_ms", [0, 500], ids=["unlimited", "stricter"])
def test_postcommit_context_lock_timeout_preserves_recovery(configured_lock_ms, record_property):
    with _owned_case(configured_lock_ms, record_property) as case:
        admin, blocker, transaction, observer, channel, receipt = case
        initial = receipt["ready"]["session"]
        _assert_restored(initial, initial, configured_lock_ms)
        blocked = _phase(channel, observer, receipt, "blocked_recovery")
        witness = blocked["committed_while_blocked"]
        assert witness, "Fixture admission: committed recovery during owned lock wait not observed"
        committed = witness["state"]
        _assert_committed(committed, receipt["before"])
        outcome = blocked["outcome"]
        assert outcome["error_type"] is None and outcome["recovered"] == 1
        assert len(outcome["sql_errors"]) == 1
        error = outcome["sql_errors"][0]
        assert error["application_select"] is True
        assert error["sqlstate"] == "55P03", "Expected natural lock timeout, not fixture statement guard"
        assert outcome["elapsed_seconds"] < 4, "Fixture statement guard is not the oracle"
        _assert_local_cap(blocked["lock_session"], initial, configured_lock_ms)
        _assert_restored(blocked["restored_session"], initial, configured_lock_ms)
        assert transaction.is_active
        assert blocker.execute(text("SELECT pg_backend_pid()")).scalar_one() == receipt["blocker_pid"]
        receipt["after_timeout"] = _independent_state(admin)
        assert receipt["after_timeout"] == committed, "Enrichment failure changed committed truth"
        assert len(outcome["logs"]) == 1
        encoded = outcome["logs"][0]
        assert _PRIVATE_MARKER not in encoded
        record = json.loads(encoded)
        assert set(record) == {"timestamp", "event", "level", "operation_id", "operation_kind",
                               "worker", "duration_ms", "result"}
        assert record["event"] == "operation_finished" and record["level"] == "INFO"
        assert record["operation_id"] == "operation" and record["operation_kind"] == "APPLICATION_DRY_RUN"
        assert record["worker"] == "mjobs-worker" and record["result"] == "COMPLETED"
        assert record["duration_ms"] >= 0

        transaction.rollback()
        receipt["after_release"] = _independent_state(admin)
        receipt["after_application"] = _application_state(admin)
        assert receipt["after_release"] == committed
        assert receipt["after_application"] == receipt["before_application"]
        normal = _phase(channel, observer, receipt, "normal_context")
        assert normal["outcome"]["error_type"] is None
        assert normal["outcome"]["sql_errors"] == [] and normal["outcome"]["logs"] == []
        assert normal["outcome"]["context"] == {
            "application_id": "application", "job_id": "job", "company_id": "company",
            "ats_provider": "greenhouse",
        }
        _assert_local_cap(normal["lock_session"], initial, configured_lock_ms)
        _assert_restored(normal["restored_session"], initial, configured_lock_ms)
        receipt["after_normal"] = _independent_state(admin)
        assert receipt["after_normal"] == committed

        repeated = _phase(channel, observer, receipt, "repeat")
        assert repeated["outcome"]["error_type"] is None and repeated["outcome"]["recovered"] == 0
        assert repeated["outcome"]["sql_errors"] == [] and repeated["outcome"]["logs"] == []
        _assert_restored(repeated["restored_session"], initial, configured_lock_ms)
        receipt["after_repeat"] = _independent_state(admin)
        assert receipt["after_repeat"] == committed, "Repeated sweep duplicated state or audit"
        receipt["after_repeat_application"] = _application_state(admin)
        assert receipt["after_repeat_application"] == receipt["before_application"]
