"""T08 recovery checks for durable form-review worker receipts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
import unittest

from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.application_form_review import handle_form_review
from workers.operations import claim, recover_expired


SAFE_ANALYSIS = {
    "status": "REVIEW_REQUIRED", "reason": "FORM_REQUIRES_HUMAN_REVIEW",
    "fields": [{"id": "email", "name": "email", "type": "email",
                "required": True, "review_reason": None}],
}
GREENHOUSE_URL = "https://boards.greenhouse.io/example/jobs/12345"


class T08WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t08-worker-recovery-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 't08-worker.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture")
                       if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(self.application_id)
        self.route_url = GREENHOUSE_URL
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(provider="greenhouse", tenant="example", requisition="12345",
                     url=self.route_url))
        self.route_id = application["route_id"]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _operation(self):
        params = {
            "profile_id": "fixture", "application_id": self.application_id,
            "route_id": self.route_id, "route_url": self.route_url,
            "provider": "greenhouse", "request_id": "t08-recovery-receipt",
            "input_fingerprint": "a" * 64,
        }
        operation_id = identity("APPLICATION_FORM_REVIEW", self.application_id, params)
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id, kind="APPLICATION_FORM_REVIEW",
                entity_id=self.application_id, params=params, state="RUNNING",
                error=None, updated_at=now(), attempt_count=1, cancel_requested=False,
                lease_expires_at=(datetime.now(timezone.utc)
                                  + timedelta(minutes=2)).isoformat(),
                last_attempt_at=now(),
            ))
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one())

    def _audit_events(self, operation_id):
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings()
            return [dict(row) for row in rows
                    if row["payload"].get("operation_id") == operation_id]

    def test_committed_review_receipt_recovers_after_worker_lease_expires(self):
        operation = self._operation()
        first_fetch = Mock(return_value="<form>PRIVATE_ANSWER</form>")
        parser = Mock(return_value=deepcopy(SAFE_ANALYSIS))
        with patch("workers.application_form_review.analyze_application_form", parser):
            committed = handle_form_review(
                self.store, operation, fetch_html=first_fetch, check=lambda: None,
            )

        # Simulate interruption after the audit transaction committed, followed
        # by lease expiry before the queue runner replays the operation.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation["id"],
            ).values(lease_expires_at=(datetime.now(timezone.utc)
                                       - timedelta(minutes=1)).isoformat()))

        # Recovery must reopen durable state and reconcile the committed receipt
        # before retrying the expired attempt, regardless of the retry cap.
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.store.bind_workspace_mode("FIXTURE")
        self.assertEqual(recover_expired(self.store), 1)
        with self.store.engine.connect() as connection:
            recovered_operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation["id"],
            )).mappings().one())
        self.assertEqual(recovered_operation["state"], "COMPLETED")
        self.assertIsNone(recovered_operation["error"])
        self.assertEqual(recovered_operation["attempt_count"], 1)
        self.assertIsNone(claim(self.store, operation["id"]))

        replay_fetch = Mock(side_effect=AssertionError("recovery must use the receipt"))
        recovered = handle_form_review(
            self.store, operation, fetch_html=replay_fetch, check=lambda: None,
        )

        self.assertEqual(recovered, committed)
        replay_fetch.assert_not_called()
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(len(self._audit_events(operation["id"])), 1)
        self.assertNotIn("PRIVATE_ANSWER", json.dumps(self._audit_events(operation["id"])[0]["payload"]))


if __name__ == "__main__":
    unittest.main()
