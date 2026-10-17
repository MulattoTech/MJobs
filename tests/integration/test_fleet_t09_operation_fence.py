"""T09 physical admission must honor durable APPLY_MOCK ownership."""
import unittest
from pathlib import Path
import tempfile

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict, start_attempt
from services.applications.run_requests import queue_mock_run
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from fixtures.mock_server import fixture_server
from workers.operations import claim, request_cancel


class FleetT09OperationFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t09-fence-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.fixture = fixture_server()
        self.origin = self.fixture.__enter__()
        discover(self.store, self.origin, "fixture")
        self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"],
                              "fixture", self.root)

    def tearDown(self):
        self.store.engine.dispose()
        self.fixture.__exit__(None, None, None)
        self.temp.cleanup()

    def reserve(self, request_id):
        application = self.store.application(self.app_id)
        with self.store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"])).mappings().one())
            documents = list(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.app_id,
                m.documents.c.version == application["document_version"])).mappings())
        return start_attempt(self.store, application, route, documents,
                             request_id=request_id)

    def test_queued_intent_blocks_unowned_attempt_until_its_claimed_owner_reserves(self):
        queued = queue_mock_run(self.store, "fixture", self.app_id, request_id="owned-run")
        operation_id = queued["operation_id"]

        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_IN_PROGRESS"):
            self.reserve("unrelated-direct-request")
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_IN_PROGRESS"):
            self.reserve(operation_id)
        self.assertEqual(self.store.count("application_attempts"), 0)

        claimed = claim(self.store, operation_id)
        self.assertIsNotNone(claimed)
        attempt = self.reserve(operation_id)
        self.assertFalse(attempt["replayed"])
        self.assertEqual(self.store.count("application_attempts"), 1)

    def test_cancelled_or_cancel_requested_owner_cannot_reserve_attempt(self):
        queued = queue_mock_run(self.store, "fixture", self.app_id,
                                request_id="cancel-before-browser")
        operation_id = queued["operation_id"]
        self.assertEqual(request_cancel(self.store, operation_id)["state"], "CANCELLED")
        with self.assertRaises(AttemptConflict):
            self.reserve(operation_id)
        self.assertEqual(self.store.count("application_attempts"), 0)

        active = queue_mock_run(self.store, "fixture", self.app_id,
                                request_id="cancel-running-browser")
        active_id = active["operation_id"]
        self.assertIsNotNone(claim(self.store, active_id))
        self.assertTrue(request_cancel(self.store, active_id)["cancel_requested"])
        with self.assertRaises(AttemptConflict):
            self.reserve(active_id)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_owned_operation_must_still_match_its_frozen_input_snapshot(self):
        queued = queue_mock_run(self.store, "fixture", self.app_id,
                                request_id="changed-owned-input")
        operation_id = queued["operation_id"]
        self.assertIsNotNone(claim(self.store, operation_id))
        with self.store.engine.connect() as connection:
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())
        params = {**operation["params"], "input_fingerprint": "tampered-snapshot"}
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(params=params))

        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_INPUT_CHANGED"):
            self.reserve(operation_id)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_direct_attempt_without_durable_operation_remains_supported(self):
        attempt = self.reserve("direct-local-attempt")
        self.assertFalse(attempt["replayed"])
        self.assertEqual(self.store.count("application_attempts"), 1)


if __name__ == "__main__":
    unittest.main()
