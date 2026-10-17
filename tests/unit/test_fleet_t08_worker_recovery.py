"""T08 recovery checks for durable form-review worker receipts."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import unittest

from sqlalchemy import update

from packages.persistence import models as m
from tests.unit.test_application_form_review_worker import (
    ApplicationFormReviewWorkerTests,
)
from workers.application_form_review import handle_form_review


class T08WorkerRecoveryTests(unittest.TestCase):
    def test_committed_review_receipt_recovers_after_worker_lease_expires(self):
        fixture = ApplicationFormReviewWorkerTests(
            "test_review_persists_only_safe_report_and_replay_does_not_fetch_again",
        )
        fixture.setUp()
        try:
            operation = fixture.make_operation()
            first_fetch = Mock(return_value="<form>PRIVATE_ANSWER</form>")
            service_patch, _parser = fixture.with_analysis_service()
            with service_patch:
                committed = handle_form_review(
                    fixture.store, operation, fetch_html=first_fetch,
                    check=lambda: None,
                )

            # Simulate a worker interruption after the audit transaction committed,
            # followed by lease expiry before the queue runner replays the operation.
            with fixture.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation["id"],
                ).values(lease_expires_at=(datetime.now(timezone.utc)
                                           - timedelta(minutes=1)).isoformat()))

            replay_fetch = Mock(side_effect=AssertionError("recovery must use the receipt"))
            recovered = handle_form_review(
                fixture.store, operation, fetch_html=replay_fetch,
                check=lambda: None,
            )

            self.assertEqual(recovered, committed)
            replay_fetch.assert_not_called()
            self.assertEqual(len(fixture.audit_events(operation["id"])), 1)
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
