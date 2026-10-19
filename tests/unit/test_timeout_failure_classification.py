"""Timeout classes are classified by exact trusted type, never by name alone."""
import unittest

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.pool import QueuePool

from services.applications.attempt_history import failure_code
from workers.operations import OperationCancelled, OperationTimedOut
from workers.tasks import _classify_unexpected_failure


class TimeoutFailureClassificationTests(unittest.TestCase):
    def _real_sqlalchemy_pool_timeout(self):
        engine = create_engine(
            "sqlite://",
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.01,
        )
        held = engine.connect()
        try:
            with self.assertRaises(SQLAlchemyTimeoutError) as caught:
                engine.connect()
            return caught.exception
        finally:
            held.close()
            engine.dispose()

    def test_real_pool_timeout_has_database_code(self):
        error = self._real_sqlalchemy_pool_timeout()

        self.assertIs(type(error), SQLAlchemyTimeoutError)
        self.assertEqual(failure_code(error), "DATABASE_POOL_TIMEOUT")

    def test_playwright_timeout_has_browser_code(self):
        error = PlaywrightTimeoutError("PRIVATE_FORM_VALUE C:/private/path")

        self.assertEqual(failure_code(error), "PLAYWRIGHT_TIMEOUT")
        self.assertNotIn("PRIVATE_FORM_VALUE", failure_code(error))
        self.assertEqual(_classify_unexpected_failure(error), "PLAYWRIGHT_TIMEOUT")

    def test_builtin_and_same_name_impostor_timeouts_stay_ambiguous(self):
        impostor_type = type("TimeoutError", (Exception,), {})
        errors = [
            TimeoutError("private legacy detail"),
            impostor_type("private impostor detail"),
        ]

        for error in errors:
            with self.subTest(error_type=type(error)):
                self.assertEqual(failure_code(error), "TIMEOUT_UNCLASSIFIED")
                self.assertEqual(_classify_unexpected_failure(error), "TIMEOUT_UNCLASSIFIED")
                self.assertNotIn("private", failure_code(error).casefold())

    def test_sqlalchemy_pool_timeout_has_database_code_in_worker_classifier(self):
        error = self._real_sqlalchemy_pool_timeout()

        self.assertEqual(_classify_unexpected_failure(error), "DATABASE_POOL_TIMEOUT")

    def test_existing_typed_cancellation_and_deadline_codes_are_preserved(self):
        self.assertEqual(failure_code(OperationCancelled()), "OPERATION_CANCELLED")
        self.assertEqual(failure_code(OperationTimedOut()), "OPERATION_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
