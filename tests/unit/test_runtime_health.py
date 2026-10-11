"""Runtime readiness must reject missing and stale scheduler/worker heartbeats."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from services.runtime_health import (
    SCHEDULER_HEARTBEAT_KEY,
    publish_scheduler_heartbeat,
    read_runtime_health,
)
from workers.operations import QUEUES


class FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, *, ex):
        self.writes.append((key, value, ex))
        self.values[key] = value


class RuntimeHealthTests(unittest.TestCase):
    def test_successful_scheduler_tick_heartbeat_has_a_bounded_expiry(self):
        redis = FakeRedis()
        timestamp = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

        result = publish_scheduler_heartbeat(redis, at=timestamp)

        self.assertEqual(result, '{"timestamp":"2026-10-11T12:00:00+00:00","interval_seconds":30}')
        self.assertEqual(redis.writes, [(SCHEDULER_HEARTBEAT_KEY, result, 90)])

    def test_maximum_configured_scheduler_interval_has_a_bounded_freshness_window(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        redis = FakeRedis()
        publish_scheduler_heartbeat(redis, at=now - timedelta(seconds=600), interval_seconds=300)

        with patch("services.runtime_health.Worker.all", return_value=[]):
            still_fresh = read_runtime_health(redis, now=now)
        redis.values[SCHEDULER_HEARTBEAT_KEY] = json.dumps({
            "timestamp": (now - timedelta(seconds=631)).isoformat(),
            "interval_seconds": 300,
        })
        with patch("services.runtime_health.Worker.all", return_value=[]):
            stale = read_runtime_health(redis, now=now)

        self.assertTrue(still_fresh["scheduler"])
        self.assertFalse(stale["scheduler"])

    def test_runtime_readiness_requires_fresh_scheduler_and_every_exact_queue_worker(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        redis = FakeRedis({SCHEDULER_HEARTBEAT_KEY: json.dumps({
            "timestamp": now.isoformat(), "interval_seconds": 30,
        })})
        workers = []
        for queue in sorted(set(QUEUES.values())):
            worker = type("Worker", (), {})()
            worker.get_state = lambda: "idle"
            worker.queue_names = lambda q=queue: [q]
            worker.worker_ttl = 420
            # RQ refreshes an idle worker before blocking for worker_ttl - 15s.
            worker.last_heartbeat = now - timedelta(seconds=405)
            workers.append(worker)

        with patch("services.runtime_health.Worker.all", return_value=workers):
            result = read_runtime_health(redis, now=now)

        self.assertEqual(result, {
            "scheduler": True,
            "workers": {
                "mjobs-application": True,
                "mjobs-company-discovery": True,
                "mjobs-discovery": True,
                "mjobs-document": True,
            },
        })

    def test_missing_scheduler_and_company_worker_are_not_ready(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        redis = FakeRedis()
        worker = type("Worker", (), {})()
        worker.get_state = lambda: "idle"
        worker.queue_names = lambda: ["mjobs-application", "mjobs-discovery", "mjobs-document"]
        worker.last_heartbeat = now
        worker.worker_ttl = 420

        with patch("services.runtime_health.Worker.all", return_value=[worker]):
            result = read_runtime_health(redis, now=now)

        self.assertFalse(result["scheduler"])
        self.assertFalse(result["workers"]["mjobs-company-discovery"])
        self.assertTrue(result["workers"]["mjobs-application"])

    def test_malformed_expired_and_future_scheduler_heartbeats_are_not_ready(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        workers = []
        for value in ("not-a-timestamp", (now - timedelta(seconds=91)).isoformat(),
                      (now + timedelta(seconds=1)).isoformat()):
            timestamp = value
            if value != "not-a-timestamp":
                timestamp = json.dumps({"timestamp": value, "interval_seconds": 30})
            redis = FakeRedis({SCHEDULER_HEARTBEAT_KEY: timestamp})
            with patch("services.runtime_health.Worker.all", return_value=workers):
                result = read_runtime_health(redis, now=now)
            self.assertFalse(result["scheduler"], value)

    def test_stale_or_future_worker_heartbeat_never_counts_as_ready(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        redis = FakeRedis({SCHEDULER_HEARTBEAT_KEY: json.dumps({
            "timestamp": now.isoformat(), "interval_seconds": 30,
        })})
        workers = []
        for queue, heartbeat in (
            ("mjobs-application", now - timedelta(seconds=481)),
            ("mjobs-company-discovery", now + timedelta(seconds=1)),
        ):
            worker = type("Worker", (), {})()
            worker.get_state = lambda: "idle"
            worker.queue_names = lambda q=queue: [q]
            worker.last_heartbeat = heartbeat
            worker.worker_ttl = 420
            workers.append(worker)

        with patch("services.runtime_health.Worker.all", return_value=workers):
            result = read_runtime_health(redis, now=now)

        self.assertFalse(result["workers"]["mjobs-application"])
        self.assertFalse(result["workers"]["mjobs-company-discovery"])

    def test_workers_without_parseable_heartbeat_are_not_ready(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        redis = FakeRedis({SCHEDULER_HEARTBEAT_KEY: json.dumps({
            "timestamp": now.isoformat(), "interval_seconds": 30,
        })})
        worker_with_malformed_heartbeat = type("Worker", (), {})()
        worker_with_malformed_heartbeat.get_state = lambda: "idle"
        worker_with_malformed_heartbeat.queue_names = lambda: ["mjobs-company-discovery"]
        worker_with_malformed_heartbeat.last_heartbeat = "not-a-timestamp"
        worker_with_malformed_heartbeat.worker_ttl = 420
        worker_without_heartbeat = type("Worker", (), {})()
        worker_without_heartbeat.get_state = lambda: "idle"
        worker_without_heartbeat.queue_names = lambda: ["mjobs-application"]
        worker_without_heartbeat.worker_ttl = 420
        worker_without_heartbeat.last_heartbeat = None
        worker_with_naive_heartbeat = type("Worker", (), {})()
        worker_with_naive_heartbeat.get_state = lambda: "idle"
        worker_with_naive_heartbeat.queue_names = lambda: ["mjobs-discovery"]
        worker_with_naive_heartbeat.last_heartbeat = datetime(2026, 10, 11, 12, 0)
        worker_with_naive_heartbeat.worker_ttl = 420

        with patch("services.runtime_health.Worker.all", return_value=[
            worker_with_malformed_heartbeat, worker_without_heartbeat,
            worker_with_naive_heartbeat,
        ]):
            result = read_runtime_health(redis, now=now)

        self.assertFalse(result["workers"]["mjobs-company-discovery"])
        self.assertFalse(result["workers"]["mjobs-application"])
        self.assertFalse(result["workers"]["mjobs-discovery"])


if __name__ == "__main__":
    unittest.main()
