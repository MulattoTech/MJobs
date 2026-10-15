"""Container health probes must match this worker and its live work signal."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import yaml

from services.runtime_health import (
    SCHEDULER_HEARTBEAT_KEY,
    main,
    worker_health_probe,
)


class FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key):
        return self.values.get(key)


def worker(*, hostname, queue, state="idle", heartbeat=None, ttl=420, pid=1234):
    return type("Worker", (), {
        "hostname": hostname,
        "pid": pid,
        "get_state": lambda self: state,
        "queue_names": lambda self: [queue],
        "last_heartbeat": heartbeat,
        "worker_ttl": ttl,
    })()


class ContainerHealthTests(unittest.TestCase):
    def test_worker_probe_requires_fresh_active_worker_on_exact_queue_and_host(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        cases = (
            (worker(hostname="container-a", queue="mjobs-document", state="idle",
                    heartbeat=now), True),
            (worker(hostname="container-b", queue="mjobs-document", heartbeat=now), False),
            (worker(hostname="container-a", queue="mjobs-ai", heartbeat=now), False),
            (worker(hostname="container-a", queue="mjobs-document", heartbeat="invalid"), False),
            (worker(hostname="container-a", queue="mjobs-document",
                    heartbeat=now - timedelta(seconds=481)), False),
            (worker(hostname="container-a", queue="mjobs-document",
                    heartbeat=now + timedelta(seconds=1)), False),
            (worker(hostname="container-a", queue="mjobs-document", state="stopped",
                    heartbeat=now), False),
        )
        for registered_worker, expected in cases:
            with self.subTest(host=registered_worker.hostname,
                              queue=registered_worker.queue_names()[0],
                              state=registered_worker.get_state()):
                with patch("services.runtime_health.Worker.all", return_value=[registered_worker]), \
                        patch("services.runtime_health._worker_pid_exists_in_container", return_value=True):
                    self.assertIs(worker_health_probe(
                        FakeRedis(), "mjobs-document", "container-a", now=now,
                    ), expected)

    def test_worker_probe_fails_closed_on_redis_errors_and_invalid_configuration(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        with patch("services.runtime_health.Worker.all", side_effect=TimeoutError("private redis URL")):
            self.assertFalse(worker_health_probe(
                FakeRedis(), "mjobs-document", "container-a", now=now,
            ))
        for queue, hostname in (("unknown", "container-a"), ("mjobs-document", "")):
            self.assertFalse(worker_health_probe(FakeRedis(), queue, hostname, now=now))

    def test_worker_probe_rejects_same_host_worker_when_container_pid_is_gone(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        stale_worker = worker(
            hostname="container-a", queue="mjobs-document", heartbeat=now, pid=56789,
        )
        with patch("services.runtime_health.Worker.all", return_value=[stale_worker]), \
                patch("pathlib.Path.is_dir", return_value=False):
            self.assertFalse(worker_health_probe(
                FakeRedis(), "mjobs-document", "container-a", now=now,
            ))

    def test_worker_probe_rejects_invalid_container_pid(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        malformed_worker = worker(
            hostname="container-a", queue="mjobs-document", heartbeat=now, pid=0,
        )
        with patch("services.runtime_health.Worker.all", return_value=[malformed_worker]), \
                patch("pathlib.Path.is_dir", return_value=True):
            self.assertFalse(worker_health_probe(
                FakeRedis(), "mjobs-document", "container-a", now=now,
            ))

    def test_pid_probe_fails_closed_outside_linux(self):
        from services.runtime_health import _worker_pid_exists_in_container

        with patch("sys.platform", "win32"), patch("pathlib.Path.is_dir", return_value=True):
            self.assertFalse(_worker_pid_exists_in_container(1234))

    def test_worker_cli_uses_bounded_redis_timeouts_and_closes_connection(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        registered_worker = worker(
            hostname="container-a", queue="mjobs-document", state="busy", heartbeat=now,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "MJOBS_REDIS_URL": "redis://private-host:6379/0",
            "MJOBS_WORKER_QUEUE": "mjobs-document",
        }), patch("services.runtime_health.Redis.from_url", return_value=connection) as from_url, \
                patch("services.runtime_health.socket.gethostname", return_value="container-a"), \
                patch("services.runtime_health._worker_pid_exists_in_container", return_value=True), \
                patch("services.runtime_health.Worker.all", return_value=[registered_worker]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["worker"])

        self.assertEqual(result, 0)
        self.assertEqual(from_url.call_count, 1)
        self.assertLessEqual(from_url.call_args.kwargs["socket_connect_timeout"], 2)
        self.assertLessEqual(from_url.call_args.kwargs["socket_timeout"], 2)
        connection.close.assert_called_once_with()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_fails_silently_and_closes_connection_after_redis_timeout(self):
        connection = MagicMock()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "MJOBS_REDIS_URL": "redis://private-host:6379/0",
            "MJOBS_WORKER_QUEUE": "mjobs-document",
        }), patch("services.runtime_health.Redis.from_url", return_value=connection), \
                patch("services.runtime_health.Worker.all", side_effect=TimeoutError("secret detail")), \
                patch("services.runtime_health.socket.gethostname", return_value="container-a"), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["worker"])

        self.assertEqual(result, 1)
        connection.close.assert_called_once_with()
        self.assertNotIn("secret", stdout.getvalue() + stderr.getvalue())

    def test_scheduler_cli_uses_the_fresh_success_heartbeat_and_closes_connection(self):
        now = datetime.now(timezone.utc)
        connection = MagicMock()
        connection.get.return_value = json.dumps({
            "timestamp": now.isoformat(), "interval_seconds": 30,
        })
        with patch.dict(os.environ, {"MJOBS_REDIS_URL": "redis://redis:6379/0"}), \
                patch("services.runtime_health.Redis.from_url", return_value=connection):
            result = main(["scheduler"])

        self.assertEqual(result, 0)
        connection.get.assert_called_once_with(SCHEDULER_HEARTBEAT_KEY)
        connection.close.assert_called_once_with()

    def test_compose_workers_and_scheduler_use_the_health_probe(self):
        compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        for name in ("worker", "source-worker", "company-discovery-worker", "ai-worker", "document-worker"):
            with self.subTest(service=name):
                service = services[name]
                self.assertEqual(service["healthcheck"]["test"], [
                    "CMD", "python", "-m", "services.runtime_health", "worker",
                ])
                self.assertEqual(service["healthcheck"]["timeout"], "4s")
                self.assertGreaterEqual(
                    int(service["healthcheck"]["start_period"].rstrip("s")), 30,
                )
                self.assertEqual(service["healthcheck"]["retries"], 5)
        scheduler = services["scheduler"]
        self.assertEqual(scheduler["healthcheck"]["test"], [
            "CMD", "python", "-m", "services.runtime_health", "scheduler",
        ])
        self.assertEqual(scheduler["healthcheck"]["timeout"], "4s")
        self.assertGreaterEqual(
            int(scheduler["healthcheck"]["start_period"].rstrip("s")), 30,
        )
        self.assertEqual(scheduler["healthcheck"]["retries"], 5)

    def test_api_compose_healthcheck_uses_full_readiness(self):
        compose = yaml.safe_load(Path("compose.yml").read_text(encoding="utf-8"))
        api_health = compose["services"]["api"]["healthcheck"]
        self.assertIn("/ready", api_health["test"][-1])
        self.assertNotIn("/health'", api_health["test"][-1])
        self.assertEqual(api_health["timeout"], "4s")
        self.assertEqual(api_health["retries"], 5)
        self.assertEqual(api_health["start_period"], "30s")


if __name__ == "__main__":
    unittest.main()
