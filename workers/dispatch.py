"""Durable database outbox with bounded Redis/RQ dispatch."""
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.job import Job
from rq.exceptions import NoSuchJobError
from sqlalchemy import case, or_, select, text

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.audit_history import record_application_preparation_event
from workers.operations import QUEUES, recover_expired, timeout


def enqueue_operation(store, kind, entity_id, params, *, connection=None, preparation_priority=None):
    if kind not in QUEUES:
        raise ValueError("Unsupported operation kind")
    operation_id = identity(kind, entity_id, params)
    saved_params = {**params, "preparation_priority": preparation_priority} if preparation_priority else params
    values = {"id": operation_id, "kind": kind, "entity_id": entity_id,
        "params": saved_params, "state": "QUEUED", "error": None, "updated_at": now()}
    def persist(conn):
        if kind == "PREPARE" and preparation_priority:
            # Serialize request identity per profile, including across API processes.
            if conn.dialect.name == "sqlite" and not conn.connection.driver_connection.in_transaction:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            profile_version = conn.execute(select(m.profiles.c.version).where(
                m.profiles.c.id == entity_id).with_for_update()).scalar_one()
            if params.get("request_id"):
                prior = conn.execute(select(m.operations).where(
                    m.operations.c.kind == kind, m.operations.c.entity_id == entity_id,
                    m.operations.c.params["request_id"].as_string() == params["request_id"],
                )).mappings().first()
                if prior:
                    original = {k: v for k, v in prior["params"].items()
                                if k not in {"preparation_priority", "pipeline_workbench_action_fingerprint"}}
                    if original != params:
                        raise ValueError("PREPARATION_REQUEST_CONFLICT")
                    return prior["id"]
            if conn.execute(select(m.operations.c.id).where(
                m.operations.c.kind == "DISCOVER", m.operations.c.entity_id == entity_id,
                m.operations.c.state.in_(["QUEUED", "RUNNING"]),
                m.operations.c.params["rescore_only"].as_boolean() == True,
            ).limit(1)).scalar_one_or_none():
                raise ValueError("PROFILE_SCORING_IN_PROGRESS")
            if profile_version != preparation_priority["profile_version"]:
                raise ValueError("PREPARATION_INPUT_CHANGED")
            from services.documents.tailor import target_fingerprint
            job = conn.execute(select(m.jobs).where(
                m.jobs.c.id == params["job_id"]).with_for_update()).mappings().one()
            if target_fingerprint(job) != preparation_priority["job_fingerprint"]:
                raise ValueError("PREPARATION_INPUT_CHANGED")
        upsert(conn, m.operations, values)
        _record_queued_preparation(conn, kind, entity_id, params, operation_id)
        return operation_id
    if connection is None:
        with store.engine.begin() as owned_connection:
            return persist(owned_connection)
    else:
        return persist(connection)


def _record_queued_preparation(connection, kind, profile_id, params, operation_id):
    if kind == "PREPARE" and isinstance(params.get("job_id"), str):
        record_application_preparation_event(
            connection, "APPLICATION_QUEUED", profile_id,
            params["job_id"], operation_id)


def _pending_operations(connection):
    """Select durable outbox work with explicit high-priority discovery first."""
    priority = case(
        (m.operations.c.params["dispatch_priority"].as_string() == "HIGH", 0),
        else_=1,
    )
    rows = list(connection.execute(select(m.operations).where(
        m.operations.c.state == "QUEUED",
        or_(m.operations.c.next_attempt_at.is_(None), m.operations.c.next_attempt_at <= now()),
    ).order_by(priority, m.operations.c.updated_at, m.operations.c.id)).mappings())
    from services.matching.preparation_priority import scheduling_key
    timestamp = now()
    preparations = iter(sorted((row for row in rows if row["kind"] == "PREPARE"),
                               key=lambda row: scheduling_key(row, timestamp)))
    return [next(preparations) if row["kind"] == "PREPARE" else row for row in rows]


def dispatch_pending(store, redis_url):
    recover_expired(store)
    redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        redis.ping()
        # One dispatcher owns the outbox at a time, including the Redis enqueue.
        with store.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                if not connection.execute(text("SELECT pg_try_advisory_xact_lock(649106602)")).scalar_one():
                    return {"status": "DISPATCHER_ACTIVE", "count": 0}
            elif connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            rows = _pending_operations(connection)
            count = 0
            for row in rows:
                try:
                    job = Job.fetch(row["id"], connection=redis)
                    if job.get_status() in ("queued", "started", "scheduled", "deferred"):
                        continue
                    job.delete()
                except NoSuchJobError:
                    pass
                Queue(QUEUES[row["kind"]], connection=redis).enqueue("workers.tasks.execute_operation", row["id"], job_id=row["id"], job_timeout=timeout(),
                              result_ttl=86400, failure_ttl=86400)
                count += 1
        return {"status": "DISPATCHED", "count": count}
    except RedisError:
        return {"status": "QUEUE_UNAVAILABLE", "count": 0}
    finally:
        redis.close()
