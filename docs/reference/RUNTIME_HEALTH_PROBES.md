# Maintainer reference: runtime health probes

**Source:** `services/runtime_health.py`
**Current main source commit:** `dec30c341f96fcb719458e6ecacb172904d93976`
**Source blob:** `7401dd076ab0de154ebfa0e65a6b7707bc07a284`

## What this module reports

This module provides three related but distinct observations: `scheduler_health_probe` checks one Redis scheduler-success heartbeat; `worker_health_probe` checks one named queue and this container’s worker; `read_runtime_health` builds the aggregate readiness mapping for every queue in `REQUIRED_QUEUES` (`services/runtime_health.py:16–24, 105–166`; the `read_runtime_health` definition in that module). The module CLI selects the scheduler probe or the worker/container probe (`services/runtime_health.py:169–200`). It is a health/readiness signal, not proof of job completion, durable business outcome, or host restart.

`services/runtime_health.py` imports `QUEUES` from `workers.operations` (`services/runtime_health.py:16`); the operation-to-queue mapping is defined in `workers/operations.py:18–23`. `REQUIRED_QUEUES` is the sorted unique set of those values (`services/runtime_health.py:24`), so callers should not hard-code a separate queue list. Aggregate output has the shape `{"scheduler": bool, "workers": {queue_name: bool, ...}}` (the `read_runtime_health` definition in `services/runtime_health.py`).

## Scheduler heartbeat contract

After a complete successful scheduler tick, `publish_scheduler_heartbeat(redis_connection, *, at=None, interval_seconds=30)` stores JSON under `mjobs:runtime:scheduler:last_success` with an ISO timestamp and interval; Redis expiry is `2 * interval_seconds + 30` seconds (`services/runtime_health.py:19–24, 56–80`). Intervals must be exact integers from 5 through 300; invalid values and timezone-naive timestamps raise `ValueError` (`services/runtime_health.py:56–70`). The function returns the serialized value written to Redis.

The scheduler probe accepts ASCII bytes or a JSON string containing an object with a valid interval and timezone-aware timestamp. A timestamp is fresh only if it is not in the future and is no older than the computed TTL (`services/runtime_health.py:27–53, 83–116`). Missing, malformed, stale/future, or inaccessible heartbeats produce `False`; Redis exceptions are swallowed by this probe (`services/runtime_health.py:83–116`).

Synthetic example using a test/fake Redis connection:

```python
from datetime import datetime, timezone
from services.runtime_health import publish_scheduler_heartbeat, scheduler_health_probe

now = datetime.now(timezone.utc)
publish_scheduler_heartbeat(fake_redis, at=now, interval_seconds=30)
assert scheduler_health_probe(fake_redis, now=now)
```

The repository tests use a fake Redis object and verify the 90-second default TTL and freshness boundary (`tests/unit/test_runtime_health.py:15–36, 38–53`). Do not publish a heartbeat merely to make health green: the writer’s contract is to call it only after a complete successful tick (docstring at `services/runtime_health.py:63–67`).

## Worker and aggregate readiness rules

`worker_health_probe(redis_connection, queue, hostname, *, now=None)` rejects unknown queues, empty or non-string hostnames, and naive reference times. It requires a registered worker with the exact hostname and queue, a Linux `/proc/<pid>` directory for its positive integer PID, `idle` or `busy` state, positive integer worker TTL, and a parseable heartbeat between now and `worker_ttl + 60` seconds old (`services/runtime_health.py:119–166`, using freshness rules at `services/runtime_health.py:27–53`). Unsupported platforms or invalid PIDs fail closed; probe/query or per-worker errors also fail closed or skip that worker (`services/runtime_health.py:119–126, 139–166`).

`read_runtime_health(redis_connection, *, now=None)` checks scheduler freshness plus whether at least one registered `idle`/`busy` worker with a positive integer TTL and fresh heartbeat serves each required queue. Its worker aggregation does not perform the CLI probe’s hostname or `/proc` PID test; it is a queue-level registry/heartbeat observation (the `read_runtime_health` definition versus `worker_health_probe` in `services/runtime_health.py`). Missing queues stay `False`. Redis read and RQ enumeration errors are converted to false readiness; malformed worker state/heartbeat/queue data is skipped (the `read_runtime_health` definition in `services/runtime_health.py`). The optional `now` should be timezone-aware; this function raises `ValueError` if `tzinfo` is absent (the same definition).

Tests explicitly exercise fresh/stale scheduler and worker timestamps, future timestamps, missing queues, and the exact all-queues response shape (`tests/unit/test_runtime_health.py:55–82, 84–135`). Container-specific tests cover hostname/PID checks and non-Linux fail-closed behavior (`tests/unit/test_container_health.py:35–100`).

## Container health-check CLI

Run `python -m services.runtime_health worker` or `python -m services.runtime_health scheduler`; argparse limits the role to those choices (`services/runtime_health.py:169–173`). `MJOBS_REDIS_URL` is required; the worker role optionally uses `MJOBS_WORKER_QUEUE`, defaulting to `mjobs-application`. The worker command uses `socket.gethostname()` and the container-local probe. Both roles create a Redis client with one-second connect/read timeouts, return exit status 0 only when ready and 1 for missing configuration, connection/probe failure, or not-ready state; they emit no diagnostic output and close the client in `finally` (`services/runtime_health.py:174–200`). Invalid CLI arguments follow argparse’s normal error/SystemExit behavior (`services/runtime_health.py:171–173`).

Safe local usage is to let the configured Compose healthcheck invoke these commands against its own `MJOBS_REDIS_URL`; do not paste real connection strings into logs or examples. The unit tests verify bounded timeouts, quiet failure, and connection closure (`tests/unit/test_container_health.py:102–157`); the adjacent test asserts the worker Compose healthcheck command and bounds (`tests/unit/test_container_health.py:159–170`).

## Existing documentation and gap assessment

`docs/TROUBLESHOOTING.md` warns that a running container is not proof of a fresh worker heartbeat and links the readiness endpoint/runtime-health module (`docs/TROUBLESHOOTING.md:40–54`). `docs/ARCHITECTURE.md` describes runtime health at a high level (`docs/ARCHITECTURE.md:257–263`). These are useful operational pointers but do not explain the three probe contracts, heartbeat TTL/format, CLI environment and exit behavior, or the aggregate-vs-container-local distinction. Therefore this reference records a specific maintainer-documentation gap rather than duplicating those broader procedures.

Source and cited-document references were revalidated against current main `dec30c341f96fcb719458e6ecacb172904d93976`. This reference does not establish runtime behavior or test acceptance.
