"""Scheduler startup and Redis disconnects must release acquired resources."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event

from packages.persistence.store import Store
from services.discovery import scheduler


ENVIRONMENT = {
    "MJOBS_DATABASE_URL": "sqlite://",
    "MJOBS_REDIS_URL": "redis://127.0.0.1:1/15",
    "MJOBS_OPERATION_MODE": "FIXTURE",
    "MJOBS_SCHEDULER_TICK_SECONDS": "5",
}


class DiscoverySchedulerLifecycleTests(unittest.TestCase):
    def test_invalid_intervals_do_not_allocate_store_or_redis(self):
        for name, values in (
            ("MJOBS_SCHEDULER_TICK_SECONDS", ("invalid", "4", "301")),
            (scheduler.RETENTION_INTERVAL_ENV, ("invalid", "299", "2592001")),
        ):
            for value in values:
                with self.subTest(setting=name, value=value), \
                        patch.dict(os.environ, {**ENVIRONMENT, name: value}, clear=True), \
                        patch.object(scheduler, "Store") as create_store, \
                        patch.object(scheduler.Redis, "from_url") as create_redis:
                    with self.assertRaises(ValueError):
                        scheduler.run_forever()
                    create_store.assert_not_called()
                    create_redis.assert_not_called()

    def test_workspace_binding_failure_disposes_store_and_preserves_error(self):
        store = Mock()
        failure = RuntimeError("synthetic-binding-failure")
        store.bind_workspace_mode.side_effect = failure
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(scheduler, "Store", return_value=store), \
                patch.object(scheduler.Redis, "from_url") as create_redis, \
                patch.object(scheduler, "scheduler_tick") as tick:
            with self.assertRaises(RuntimeError) as caught:
                scheduler.run_forever()
        self.assertIs(caught.exception, failure)
        store.engine.dispose.assert_called_once_with()
        create_redis.assert_not_called()
        tick.assert_not_called()

    def test_missing_redis_configuration_disposes_bound_store(self):
        store = Mock()
        environment = {key: value for key, value in ENVIRONMENT.items()
                       if key != "MJOBS_REDIS_URL"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(scheduler, "Store", return_value=store), \
                patch.object(scheduler.Redis, "from_url") as create_redis:
            with self.assertRaises(KeyError) as caught:
                scheduler.run_forever()
        self.assertEqual(caught.exception.args, ("MJOBS_REDIS_URL",))
        store.bind_workspace_mode.assert_called_once_with("FIXTURE")
        store.engine.dispose.assert_called_once_with()
        create_redis.assert_not_called()

    def test_redis_construction_failure_disposes_real_store_and_reopen_recovers(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-scheduler-lifecycle-") as directory:
            url = "sqlite:///" + (Path(directory) / "scheduler.db").as_posix()
            store = Store(url)
            store.migrate()
            with store.engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE lifecycle_probe (value TEXT NOT NULL)")
                connection.exec_driver_sql("INSERT INTO lifecycle_probe VALUES ('retained')")
            disposed = []
            event.listen(store.engine, "engine_disposed", lambda engine: disposed.append(engine))
            failure = RedisConnectionError("synthetic-redis-startup-failure")
            environment = {**ENVIRONMENT, "MJOBS_DATABASE_URL": url}
            reopened = None
            try:
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(scheduler, "Store", return_value=store), \
                        patch.object(scheduler.Redis, "from_url", side_effect=failure), \
                        patch.object(scheduler, "scheduler_tick") as tick:
                    with self.assertRaises(RedisConnectionError) as caught:
                        scheduler.run_forever()
                self.assertIs(caught.exception, failure)
                self.assertEqual(disposed, [store.engine])
                tick.assert_not_called()

                reopened = Store(url)
                recovered_disposals = []
                event.listen(reopened.engine, "engine_disposed",
                             lambda engine: recovered_disposals.append(engine))
                redis = Mock()
                stop = SystemExit(0)
                with patch.dict(os.environ, environment, clear=True), \
                        patch.object(scheduler, "Store", return_value=reopened), \
                        patch.object(scheduler.Redis, "from_url", return_value=redis) as create_redis, \
                        patch.object(scheduler, "scheduler_tick", return_value={}) as tick, \
                        patch.object(scheduler, "run_transient_retention_if_configured") as retention, \
                        patch.object(scheduler.time, "sleep", side_effect=stop):
                    with self.assertRaises(SystemExit) as caught:
                        scheduler.run_forever()
                self.assertIs(caught.exception, stop)
                tick.assert_called_once_with(reopened, redis, interval_seconds=5)
                retention.assert_called_once_with(store=reopened)
                create_redis.assert_called_once_with(ENVIRONMENT["MJOBS_REDIS_URL"],
                                                     socket_connect_timeout=2, socket_timeout=2)
                redis.close.assert_called_once_with()
                self.assertEqual(recovered_disposals, [reopened.engine])
                with reopened.engine.connect() as connection:
                    self.assertEqual(connection.exec_driver_sql(
                        "SELECT value FROM lifecycle_probe").scalar_one(), "retained")
            finally:
                if reopened is not None:
                    reopened.engine.dispose()
                store.engine.dispose()

    def test_redis_cleanup_disconnect_preserves_shutdown_and_logs_no_error_details(self):
        for stop in (KeyboardInterrupt("synthetic-stop"), SystemExit(7)):
            with self.subTest(shutdown=type(stop).__name__):
                store, redis = Mock(), Mock()
                error_type = type("SyntheticCleanupTypeCanary", (RedisConnectionError,), {})
                failure = error_type("synthetic-cleanup-message-canary")
                failure.__cause__ = ValueError("synthetic-cleanup-cause-canary")
                failure.add_note("synthetic-cleanup-note-canary")
                redis.close.side_effect = failure
                with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                        patch.object(scheduler, "Store", return_value=store), \
                        patch.object(scheduler.Redis, "from_url", return_value=redis), \
                        patch.object(scheduler, "scheduler_tick", side_effect=stop), \
                        self.assertLogs(level="WARNING") as captured:
                    with self.assertRaises(type(stop)) as caught:
                        scheduler.run_forever()
                self.assertIs(caught.exception, stop)
                redis.close.assert_called_once_with()
                store.engine.dispose.assert_called_once_with()
                self.assertEqual(len(captured.records), 1)
                record = captured.records[0]
                self.assertEqual(record.getMessage(), "Scheduler Redis connection cleanup failed")
                self.assertFalse(record.args)
                self.assertIsNone(record.exc_info)
                self.assertIsNone(record.exc_text)
                self.assertIsNone(record.stack_info)

    def test_unexpected_redis_close_error_still_disposes_store(self):
        store, redis = Mock(), Mock()
        failure = RuntimeError("synthetic-unexpected-cleanup-failure")
        redis.close.side_effect = failure
        with patch.dict(os.environ, ENVIRONMENT, clear=True), \
                patch.object(scheduler, "Store", return_value=store), \
                patch.object(scheduler.Redis, "from_url", return_value=redis), \
                patch.object(scheduler, "scheduler_tick", side_effect=SystemExit(0)):
            with self.assertRaises(RuntimeError) as caught:
                scheduler.run_forever()
        self.assertIs(caught.exception, failure)
        store.engine.dispose.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
