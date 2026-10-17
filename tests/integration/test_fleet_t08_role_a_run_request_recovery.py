"""Role-A regression coverage for exact recovery after route drift."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict
from services.applications.run_requests import (
    queue_mock_run,
    read_mock_run,
    validate_run_operation,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class FleetT08RoleARunRequestRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t08-run-recovery-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'run.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.application_id = prepare(
            self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root,
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_route_drift_blocks_execution_and_replay_but_exact_read_recovers_original(self):
        queued = queue_mock_run(
            self.store, "fixture", self.application_id,
            request_id="t08-route-drift-recovery",
        )
        with self.store.engine.begin() as connection:
            route_id = connection.execute(select(m.applications.c.route_id).where(
                m.applications.c.id == self.application_id,
            )).scalar_one()
            connection.execute(update(m.routes).where(
                m.routes.c.id == route_id,
            ).values(url="http://127.0.0.1:1/changed"))
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == queued["operation_id"],
            )).mappings().one())

        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_INPUT_CHANGED"):
            validate_run_operation(self.store, operation)
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_REQUEST_REUSED"):
            queue_mock_run(
                self.store, "fixture", self.application_id,
                request_id="t08-route-drift-recovery",
            )

        recovered = read_mock_run(
            self.store, "fixture", self.application_id, "t08-route-drift-recovery",
        )
        self.assertEqual(recovered["operation_id"], queued["operation_id"])
        self.assertEqual(recovered["input_fingerprint"], queued["input_fingerprint"])
        self.assertTrue(recovered["replayed"])
        self.assertEqual(self.store.count("worker_operations"), 1)


if __name__ == "__main__":
    unittest.main()
