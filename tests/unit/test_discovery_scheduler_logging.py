"""Scheduler retries preserve health semantics without logging exception secrets."""
import logging
import os
import unittest
from unittest.mock import Mock, call, patch

from services.discovery import scheduler


class DiscoverySchedulerLoggingTests(unittest.TestCase):
    def _run_two_ticks(self, first_result, expected_heartbeats):
        store = Mock()
        redis = Mock()
        environment = {
            "MJOBS_DATABASE_URL": "sqlite://",
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/15",
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_SCHEDULER_TICK_SECONDS": "5",
        }
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(scheduler, "Store", return_value=store), \
                patch.object(scheduler.Redis, "from_url", return_value=redis), \
                patch.object(scheduler, "schedule_due_sources", side_effect=[first_result, {}]) as schedule, \
                patch.object(scheduler, "publish_scheduler_heartbeat") as heartbeat, \
                patch.object(scheduler, "run_transient_retention_if_configured") as retention, \
                patch.object(scheduler.time, "monotonic", side_effect=[0.0, 5.0]), \
                patch.object(scheduler.time, "sleep", side_effect=[None, SystemExit(0)]) as sleep:
            with self.assertRaises(SystemExit) as stopped:
                scheduler.run_forever()

        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(schedule.call_args_list, [call(store, None), call(store, None)])
        self.assertEqual(heartbeat.call_args_list, [
            call(redis, at=None, interval_seconds=5),
        ] * expected_heartbeats)
        self.assertEqual(sleep.call_args_list, [call(5), call(5)])
        retention.assert_called_once_with(store=store)
        store.bind_workspace_mode.assert_called_once_with("FIXTURE")
        redis.close.assert_called_once_with()
        store.engine.dispose.assert_called_once_with()

    def _assert_safe_retry(self, failure, canaries):
        with self.assertLogs(level="DEBUG") as captured:
            self._run_two_ticks(failure, expected_heartbeats=1)

        rendered = "\n".join(captured.output)
        for canary in canaries:
            self.assertNotIn(canary, rendered)
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertEqual(record.getMessage(),
                         "SCHEDULER_TICK_FAILED: retrying without losing durable state")
        self.assertFalse(record.args)
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.exc_text)
        self.assertIsNone(record.stack_info)

    def test_exception_message_is_not_logged_and_next_tick_recovers(self):
        canary = "synthetic-scheduler-secret-message-canary"
        self._assert_safe_retry(RuntimeError(canary), [canary])

    def test_exception_cause_notes_and_type_are_not_logged(self):
        message = "synthetic-scheduler-secret-message-canary"
        cause = "synthetic-scheduler-secret-cause-canary"
        note = "synthetic-scheduler-secret-note-canary"
        error_type = "SyntheticSchedulerSecretTypeCanary"
        failure = type(error_type, (RuntimeError,), {})(message)
        failure.__cause__ = ValueError(cause)
        failure.add_note(note)

        self._assert_safe_retry(failure, [message, cause, note, error_type])

    def test_successful_ticks_do_not_log_a_failure(self):
        with self.assertNoLogs(level="DEBUG"):
            self._run_two_ticks({}, expected_heartbeats=2)


if __name__ == "__main__":
    unittest.main()
