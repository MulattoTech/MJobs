"""Durable database outbox with bounded Redis/RQ dispatch."""
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.job import Job
from rq.exceptions import NoSuchJobError
from sqlalchemy import select, text

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from workers.operations import QUEUES, recover_expired, timeout


def enqueue_operation(store, kind, entity_id, params, *, connection=None):
    if kind not in QUEUES:
        raise ValueError("Unsupported operation kind")
    operation_id = identity(kind, entity_id, params)
    values = {"id": operation_id, "kind": kind, "entity_id": entity_id,
        "params": params, "state": "QUEUED", "error": None, "updated_at": now()}
    if connection is None:
        with store.engine.begin() as owned_connection:
            upsert(owned_connection, m.operations, values)
    else:
        upsert(connection, m.operations, values)
    return operation_id


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
            rows = list(connection.execute(select(m.operations).where(m.operations.c.state == "QUEUED")).mappings())
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
