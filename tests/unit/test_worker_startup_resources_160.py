"""MJ-REQ-160: every acquired worker Store is released on startup exit."""
import builtins
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import event

from packages.persistence.store import Store
from workers import main as entrypoint


class WorkerStartupResourceTests(unittest.TestCase):
    def exercise(self, stage=None, failure=None):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "worker.sqlite3"
            store = Store("sqlite:///" + str(database))
            with store.engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE lifecycle_marker (value TEXT)")
                connection.exec_driver_sql(
                    "INSERT INTO lifecycle_marker VALUES ('synthetic durable marker')"
                )
            disposals = []
            record_disposal = disposals.append
            event.listen(store.engine, "engine_disposed", record_disposal)
            stopped, thread, redis, worker = Mock(), Mock(), Mock(), Mock()
            thread_factory = Mock(return_value=thread)
            event_factory = Mock(return_value=stopped)
            dependencies = {
                "Store": Mock(return_value=store),
                "configured_submission_policy": Mock(),
                "configured_extension_registry_module": Mock(return_value=None),
                "configured_source_registry": Mock(),
                "configured_application_registry": Mock(),
                "timeout": Mock(),
                "category_limit": Mock(),
                "configure_operation_logger": Mock(),
                "Worker": Mock(return_value=worker),
                "threading": SimpleNamespace(Event=event_factory, Thread=thread_factory),
            }
            environment = {
                "MJOBS_DATABASE_URL": "sqlite:///" + str(database),
                "MJOBS_REDIS_URL": "redis://synthetic.invalid/0",
                "MJOBS_OPERATION_MODE": "FIXTURE",
                "MJOBS_WORKER_QUEUE": "mjobs-discovery",
                "MJOBS_WORKER_LOG_LEVEL": "WARNING",
            }
            if stage == "missing_redis_url":
                del environment["MJOBS_REDIS_URL"]
            elif stage == "invalid_queue":
                environment["MJOBS_WORKER_QUEUE"] = "synthetic-invalid-queue"
            elif stage == "event":
                event_factory.side_effect = failure
            elif stage == "thread":
                thread_factory.side_effect = failure
            elif stage in dependencies:
                dependencies[stage].side_effect = failure

            original_import = builtins.__import__

            def import_module(name, *args, **kwargs):
                if stage == "task_import" and name == "workers.tasks":
                    raise failure
                return original_import(name, *args, **kwargs)

            try:
                with ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, environment, clear=True))
                    for name, replacement in dependencies.items():
                        stack.enter_context(patch.object(entrypoint, name, replacement))
                    stack.enter_context(patch(
                        "services.applications.browser_profiles.configured_browser_profile"
                    ))
                    stack.enter_context(patch.object(
                        store, "bind_workspace_mode",
                        side_effect=failure if stage == "bind" else None,
                    ))
                    redis_factory = stack.enter_context(patch.object(
                        entrypoint.Redis, "from_url", return_value=redis,
                    ))
                    stack.enter_context(patch("builtins.__import__", side_effect=import_module))
                    if stage is None:
                        entrypoint.main()
                    else:
                        error_type = (
                            KeyError if stage == "missing_redis_url" else
                            ValueError if stage == "invalid_queue" else type(failure)
                        )
                        with self.assertRaises(error_type) as raised:
                            entrypoint.main()
                        if failure is not None:
                            self.assertIs(raised.exception, failure)
                        elif stage == "missing_redis_url":
                            self.assertEqual(raised.exception.args, ("MJOBS_REDIS_URL",))
                        else:
                            self.assertEqual(str(raised.exception), "Unsupported worker queue")
                        thread.start.assert_not_called()
                        redis_factory.assert_not_called()
                        worker.work.assert_not_called()

                self.assertEqual(disposals, [store.engine], "Acquired Store was not disposed once")
                # Exercise actual database reopen, not only a mocked dispose method.
                with closing(sqlite3.connect(database)) as reopened:
                    self.assertEqual(
                        reopened.execute("SELECT value FROM lifecycle_marker").fetchall(),
                        [("synthetic durable marker",)],
                    )
                if stage is None:
                    thread.start.assert_called_once_with()
                    stopped.set.assert_called_once_with()
                    thread.join.assert_called_once_with(timeout=5)
                    redis.close.assert_called_once_with()
                    worker.work.assert_called_once_with(logging_level="WARNING")
            finally:
                # Deliberately broken baseline cases must not leak fixture connections.
                try:
                    event.remove(store.engine, "engine_disposed", record_disposal)
                finally:
                    store.engine.dispose()

    def test_configuration_failures_release_acquired_store(self):
        for stage in (
            "missing_redis_url", "invalid_queue", "timeout", "category_limit",
            "configured_extension_registry_module", "configured_source_registry",
            "configured_application_registry",
        ):
            with self.subTest(stage=stage):
                failure = None if stage in ("missing_redis_url", "invalid_queue") else (
                    ValueError("synthetic configuration failure")
                )
                self.exercise(stage, failure)

    def test_early_setup_failures_release_acquired_store(self):
        for stage in ("bind", "task_import", "event", "configure_operation_logger", "thread"):
            with self.subTest(stage=stage):
                self.exercise(stage, RuntimeError("synthetic startup failure"))

    def test_early_process_exits_release_store_without_replacing_outcome(self):
        for stage in ("bind", "configure_operation_logger"):
            for failure in (KeyboardInterrupt(), SystemExit(23)):
                with self.subTest(stage=stage, outcome=type(failure).__name__):
                    self.exercise(stage, failure)

    def test_orderly_shutdown_disposes_real_store_once_and_preserves_data(self):
        self.exercise()


if __name__ == "__main__":
    unittest.main()
