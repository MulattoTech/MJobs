"""Public worker startup accepts an ordinary environment log-level setting."""
import logging
import os
import unittest
from unittest.mock import Mock, patch

from workers import main as worker_main
from workers.operations import CATEGORIES


class ConfigurationContractSustain276Tests(unittest.TestCase):
    def run_startup(self, configured_level=None, *, include_level=True):
        store = Mock()
        redis = Mock()
        worker = Mock()
        stopped = Mock()
        environment = {
            "MJOBS_DATABASE_URL": "sqlite:///configuration-test.db",
            "MJOBS_REDIS_URL": "redis://127.0.0.1:6379/0",
            "MJOBS_WORKER_QUEUE": "mjobs-application",
            "MJOBS_OPERATION_MODE": "FIXTURE",
            **{"MJOBS_" + category.upper() + "_CONCURRENCY": "1"
               for category in CATEGORIES},
        }
        if include_level:
            environment["MJOBS_WORKER_LOG_LEVEL"] = configured_level

        with patch.dict(os.environ, environment, clear=True), \
                patch("workers.main.Store", return_value=store) as store_factory, \
                patch("workers.main.Redis.from_url", return_value=redis), \
                patch("workers.main.Worker", return_value=worker), \
                patch("workers.main.threading.Event", return_value=stopped), \
                patch("workers.main.threading.Thread") as thread, \
                patch("workers.main.timeout"), \
                patch("workers.main.category_limit"), \
                patch("workers.main.configured_extension_registry_module", return_value=None), \
                patch("workers.main.configured_source_registry"), \
                patch("workers.main.configured_application_registry"), \
                patch("workers.main.configure_operation_logger"):
            if include_level and configured_level.strip().upper() not in {
                    "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                with self.assertRaisesRegex(ValueError, "^Unsupported worker log level$"):
                    worker_main.main()
                store_factory.assert_not_called()
                return None
            worker_main.main()

        return worker, redis, store, thread

    def test_default_worker_log_level_remains_warning(self):
        worker, redis, store, thread = self.run_startup(include_level=False)
        worker.work.assert_called_once_with(logging_level="WARNING")
        thread.return_value.start.assert_called_once()
        thread.return_value.join.assert_called_once_with(timeout=5)
        redis.close.assert_called_once_with()
        store.engine.dispose.assert_called_once_with()

    def test_valid_worker_log_level_is_passed_to_rq(self):
        worker, *_ = self.run_startup("DEBUG")
        worker.work.assert_called_once_with(logging_level="DEBUG")

    def test_worker_log_level_is_case_insensitive_and_trimmed(self):
        worker, *_ = self.run_startup(" eRrOr ")
        worker.work.assert_called_once_with(logging_level="ERROR")

    def test_invalid_worker_log_levels_fail_before_startup_side_effects(self):
        for level in ("TRACE", "", "20"):
            with self.subTest(level=level):
                self.assertIsNone(self.run_startup(level))


if __name__ == "__main__":
    unittest.main()
