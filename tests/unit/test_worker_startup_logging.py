"""Force recovery to finish before worker startup can configure anything else."""
from io import StringIO
import json
import logging
import os
import unittest
from unittest.mock import Mock, patch

from tests.integration import test_dry_run_receipt_recovery as receipt_contract
from workers import main as entrypoint
from workers.operations import claim
from workers.structured_logging import configure_operation_logger


class WorkerStartupLoggingTests(unittest.TestCase):
    setUp = receipt_contract.DryRunReceiptRecoveryTests.setUp
    row = receipt_contract.DryRunReceiptRecoveryTests.row
    save_receipt = receipt_contract.DryRunReceiptRecoveryTests.save_receipt
    expire = receipt_contract.DryRunReceiptRecoveryTests.expire

    def run_startup(self, *, work_error=None):
        self.output = StringIO()
        self.events = []
        stopped = Mock()
        stopped.is_set.side_effect = [False, True]
        dispatcher_redis, worker_redis = Mock(), Mock()
        thread = Mock()
        worker = Mock()
        worker.work.side_effect = work_error
        sweep_finished = False

        def inline_thread(*, target, daemon):
            self.assertTrue(daemon)

            def start():
                nonlocal sweep_finished
                self.events.append("thread_started")
                # Run the real first dispatch/recovery synchronously. Startup
                # cannot reach its next statement until this sweep has ended.
                target()
                sweep_finished = True
                self.events.append("sweep_finished")

            thread.start.side_effect = start
            return thread

        def redis_connection(_url, *, socket_connect_timeout, socket_timeout):
            if socket_connect_timeout == 2:
                self.assertEqual(socket_timeout, 2)
                return dispatcher_redis
            self.assertEqual(socket_connect_timeout, 5)
            self.assertEqual(socket_timeout, 10)
            self.assertTrue(sweep_finished, "first sweep did not finish before worker initialization")
            self.events.append("worker_redis_opened")
            return worker_redis

        def configure():
            self.events.append("logger_configured")
            configure_operation_logger(self.output)

        environment = {
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/1",
            "MJOBS_WORKER_QUEUE": "mjobs-application",
            **{"MJOBS_" + category.upper() + "_CONCURRENCY": "1"
               for category in entrypoint.CATEGORIES},
        }
        logger = logging.getLogger("mjobs.operations")
        previous_level = logger.level
        # Isolate real logger initialization without closing the caller's
        # handlers. Default WARNING must discard INFO until startup configures it.
        with patch.object(logger, "handlers", []), patch.object(logger, "disabled", False), \
                patch.object(logger, "propagate", False), \
                patch.dict(os.environ, environment), \
                patch("workers.main.configure_operation_logger", side_effect=configure), \
                patch("workers.main.threading.Event", return_value=stopped), \
                patch("workers.main.threading.Thread", side_effect=inline_thread), \
                patch("workers.main.Redis.from_url", side_effect=redis_connection), \
                patch("workers.main.Worker", return_value=worker):
            logger.setLevel(logging.WARNING)
            try:
                entrypoint.main()
            finally:
                for handler in logger.handlers:
                    handler.close()
                logger.setLevel(previous_level)
                self.worker_redis = worker_redis
                self.dispatcher_redis = dispatcher_redis
                self.stopped = stopped
                self.thread = thread

    def test_first_sweep_records_committed_completion_before_worker_initializes(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()
        self.run_startup()
        self.assertEqual(self.row()["state"], "COMPLETED")
        records = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"], "operation_finished")
        self.assertEqual(records[0]["operation_id"], self.operation_id)
        self.assertEqual(records[0]["result"], "COMPLETED")
        self.assertLess(self.events.index("logger_configured"), self.events.index("thread_started"))
        self.assertLess(self.events.index("sweep_finished"), self.events.index("worker_redis_opened"))
        self.worker_redis.close.assert_called_once_with()
        self.dispatcher_redis.close.assert_called_once_with()
        self.thread.join.assert_called_once_with(timeout=5)

    def test_repeated_startup_does_not_emit_a_second_terminal_event(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()
        self.run_startup()
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)
        self.run_startup()
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.row()["attempt_count"], 1)

    def test_live_owner_is_not_completed_or_logged_on_startup(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.run_startup()
        self.assertEqual(self.output.getvalue(), "")
        self.assertEqual(self.row()["state"], "RUNNING")

    def test_worker_initialization_failure_keeps_completed_event_and_cleans_up(self):
        self.assertIsNotNone(claim(self.store, self.operation_id))
        self.save_receipt()
        self.expire()
        with self.assertRaisesRegex(RuntimeError, "synthetic worker startup failure"):
            self.run_startup(work_error=RuntimeError("synthetic worker startup failure"))
        self.assertEqual(self.row()["state"], "COMPLETED")
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)
        self.stopped.set.assert_called_once_with()
        self.thread.join.assert_called_once_with(timeout=5)
        self.worker_redis.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
