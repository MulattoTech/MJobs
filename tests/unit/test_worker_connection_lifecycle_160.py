"""MJ-REQ-160: worker queue failures release owned startup resources.

Actual dispatcher threads; synthetic Store, Redis and RQ boundaries. No sockets,
external jobs, application retries or production worker lifecycle are exercised.
"""
from contextlib import ExitStack
import os
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from redis.exceptions import ConnectionError as RedisConnectionError

from workers import main as entrypoint


class WorkerConnectionLifecycleTests(unittest.TestCase):
    def startup(self, *, factory_error=None, work_error=None, close_error=None,
                start_error=None):
        self.store = Mock()
        self.redis = Mock()
        self.worker = Mock()
        self.stopped = Event()
        self.dispatched = Event()
        self.threads = []
        self.redis.close.side_effect = close_error
        self.worker.work.side_effect = work_error

        def cleanup_threads():
            # Bound the deliberately broken baseline too: never leak a test
            # dispatcher when an assertion or the production cleanup fails.
            self.stopped.set()
            for thread in self.threads:
                if thread.ident is not None:
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive(), "Test dispatcher leaked")

        self.addCleanup(cleanup_threads)

        def dispatch(*_args, **_kwargs):
            self.dispatched.set()

        def new_thread(**kwargs):
            if start_error is not None:
                thread = Mock()
                thread.start.side_effect = start_error
                self.failed_thread = thread
                return thread
            thread = Thread(**kwargs)
            self.threads.append(thread)
            return thread

        def connect(*_args, **_kwargs):
            self.assertTrue(self.dispatched.wait(2), "Dispatcher did not start")
            if factory_error is not None:
                raise factory_error
            return self.redis

        environment = {
            "MJOBS_DATABASE_URL": "sqlite:///unused-worker-lifecycle.db",
            "MJOBS_REDIS_URL": "redis://synthetic.invalid/0",
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_WORKER_QUEUE": "mjobs-discovery",
            "MJOBS_WORKER_LOG_LEVEL": "WARNING",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            for name, replacement in (
                ("Store", Mock(return_value=self.store)),
                ("Worker", Mock(return_value=self.worker)),
                ("dispatch_pending", dispatch),
                ("threading", SimpleNamespace(Event=lambda: self.stopped,
                                               Thread=new_thread)),
                ("configure_operation_logger", Mock()),
            ):
                stack.enter_context(patch.object(entrypoint, name, replacement))
            self.worker_factory = entrypoint.Worker
            self.redis_factory = stack.enter_context(
                patch.object(entrypoint.Redis, "from_url", side_effect=connect))
            entrypoint.main()

    def assert_released(self):
        self.assertTrue(self.stopped.is_set())
        self.assertTrue(all(not thread.is_alive() for thread in self.threads))
        self.store.engine.dispose.assert_called_once_with()

    def test_redis_factory_failure_stops_real_dispatcher_and_releases_store(self):
        failure = RedisConnectionError("synthetic factory disconnect")
        with self.assertRaises(RedisConnectionError) as raised:
            self.startup(factory_error=failure)
        self.assertIs(raised.exception, failure)
        self.assert_released()
        self.worker_factory.assert_not_called()
        self.redis.close.assert_not_called()

    def test_redis_close_outage_does_not_fail_orderly_worker_shutdown(self):
        with self.assertLogs(level="WARNING") as logs:
            self.startup(close_error=RedisConnectionError("private connection detail"))
        self.assert_released()
        self.worker.work.assert_called_once_with(logging_level="WARNING")
        self.redis.close.assert_called_once_with()
        self.assertNotIn("private connection detail", str(logs.output))

    def test_redis_close_outage_preserves_original_worker_failure(self):
        failure = RedisConnectionError("synthetic work disconnect")
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(RedisConnectionError) as raised:
                self.startup(work_error=failure,
                             close_error=RedisConnectionError("private cleanup detail"))
        self.assertIs(raised.exception, failure)
        self.assert_released()
        self.redis.close.assert_called_once_with()
        self.assertNotIn("private cleanup detail", str(logs.output))

    def test_unexpected_close_failure_still_releases_store_and_is_not_suppressed(self):
        failure = ValueError("synthetic invalid cleanup configuration")
        with self.assertRaises(ValueError) as raised:
            self.startup(close_error=failure)
        self.assertIs(raised.exception, failure)
        self.assert_released()

    def test_thread_start_failure_releases_store_without_joining_unstarted_thread(self):
        failure = RuntimeError("synthetic thread start failure")
        with self.assertRaises(RuntimeError) as raised:
            self.startup(start_error=failure)
        self.assertIs(raised.exception, failure)
        self.assert_released()
        self.failed_thread.join.assert_not_called()
        self.redis_factory.assert_not_called()
        self.worker_factory.assert_not_called()

    def test_successful_worker_shutdown_releases_all_resources_once(self):
        self.startup()
        self.assert_released()
        self.redis.close.assert_called_once_with()
        self.worker_factory.assert_called_once_with(
            ["mjobs-discovery"], connection=self.redis)


if __name__ == "__main__":
    unittest.main()
