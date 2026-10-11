"""Fresh runtime signals for the local dependency readiness endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from rq import Worker
from rq.defaults import DEFAULT_WORKER_TTL

from workers.operations import QUEUES


SCHEDULER_HEARTBEAT_KEY = "mjobs:runtime:scheduler:last_success"
SCHEDULER_MIN_INTERVAL_SECONDS = 5
SCHEDULER_MAX_INTERVAL_SECONDS = 300
SCHEDULER_HEARTBEAT_GRACE_SECONDS = 30
REQUIRED_QUEUES = tuple(sorted(set(QUEUES.values())))


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _is_fresh(value: object, *, now: datetime, max_age_seconds: int) -> bool:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return False
    age = now - timestamp
    return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)


def scheduler_heartbeat_ttl(interval_seconds: int) -> int:
    if (type(interval_seconds) is not int
            or not SCHEDULER_MIN_INTERVAL_SECONDS <= interval_seconds <= SCHEDULER_MAX_INTERVAL_SECONDS):
        raise ValueError("Scheduler interval must be from 5 through 300 seconds")
    return interval_seconds * 2 + SCHEDULER_HEARTBEAT_GRACE_SECONDS


def publish_scheduler_heartbeat(
    redis_connection, *, at: datetime | None = None, interval_seconds: int = 30,
) -> str:
    """Store a short-lived success timestamp after a complete scheduler tick."""
    ttl = scheduler_heartbeat_ttl(interval_seconds)
    timestamp = at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Scheduler heartbeat timestamp must be timezone-aware")
    value = json.dumps({
        "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
        "interval_seconds": interval_seconds,
    }, separators=(",", ":"))
    redis_connection.set(
        SCHEDULER_HEARTBEAT_KEY,
        value,
        ex=ttl,
    )
    return value


def _scheduler_is_fresh(value: object, *, now: datetime) -> bool:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(value, str):
        return False
    try:
        heartbeat = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(heartbeat, dict):
        return False
    interval = heartbeat.get("interval_seconds")
    try:
        max_age = scheduler_heartbeat_ttl(interval)
    except ValueError:
        return False
    return _is_fresh(heartbeat.get("timestamp"), now=now, max_age_seconds=max_age)


def read_runtime_health(redis_connection, *, now: datetime | None = None) -> dict:
    """Check a fresh scheduler tick and a live RQ consumer for every queue."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Runtime health reference time must be timezone-aware")
    current = current.astimezone(timezone.utc)

    scheduler = False
    try:
        scheduler = _scheduler_is_fresh(redis_connection.get(SCHEDULER_HEARTBEAT_KEY), now=current)
    except Exception:
        pass

    workers = {queue: False for queue in REQUIRED_QUEUES}
    try:
        registered_workers = Worker.all(connection=redis_connection)
    except Exception:
        registered_workers = ()
    for worker in registered_workers:
        try:
            if worker.get_state() not in ("idle", "busy"):
                continue
            worker_ttl = getattr(worker, "worker_ttl", DEFAULT_WORKER_TTL)
            if type(worker_ttl) is not int or worker_ttl <= 0:
                continue
            if not _is_fresh(getattr(worker, "last_heartbeat", None), now=current,
                             max_age_seconds=worker_ttl + 60):
                continue
            for queue in set(worker.queue_names()).intersection(workers):
                workers[queue] = True
        except Exception:
            continue
    return {"scheduler": scheduler, "workers": workers}
