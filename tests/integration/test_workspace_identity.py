"""A database cannot silently change between fictional and personal operation."""
from pathlib import Path
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from apps.api.main import create_app
from cli.workspace_identity import main as workspace_cli
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation


class WorkspaceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_empty_database_binds_once_and_reopen_rejects_mode_flip(self):
        self.store.bind_workspace_mode("PERSONAL")
        reopened = Store(self.url)
        try:
            reopened.bind_workspace_mode("PERSONAL")
            with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_MISMATCH"):
                reopened.bind_workspace_mode("FIXTURE")
        finally:
            reopened.engine.dispose()
        with self.store.engine.connect() as connection:
            rows = list(connection.execute(select(m.workspace_identity)).mappings())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["operation_mode"], "PERSONAL")

    def test_concurrent_opposite_mode_startup_has_one_winner(self):
        def bind(mode):
            store = Store(self.url)
            try:
                try:
                    store.bind_workspace_mode(mode)
                    return "BOUND", mode
                except RuntimeError as error:
                    return str(error).split(":", 1)[0], mode
            finally:
                store.engine.dispose()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(bind, ("PERSONAL", "FIXTURE")))
        self.assertEqual({outcome[0] for outcome in outcomes}, {"BOUND", "WORKSPACE_MODE_MISMATCH"})
        self.assertEqual(self.store.count("workspace_identity"), 1)

    def test_legacy_profile_without_route_is_not_silently_adopted(self):
        self.store.save_profile(PROFILE)
        with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_UNBOUND"):
            self.store.bind_workspace_mode("PERSONAL")
        with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_UNBOUND"):
            self.store.bind_workspace_mode("FIXTURE")
        self.assertEqual(self.store.count("career_profiles"), 1)
        self.assertEqual(self.store.count("workspace_identity"), 0)

    def test_explicit_legacy_adoption_requires_exact_snapshot_and_preserves_rows(self):
        self.store.save_profile(PROFILE)
        snapshot = self.store.workspace_status()
        self.assertEqual(snapshot["state"], "UNBOUND")
        self.assertEqual(snapshot["counts"]["career_profiles"], 1)
        self.assertNotIn(PROFILE["email"], str(snapshot))
        with self.assertRaisesRegex(RuntimeError, "WORKSPACE_SNAPSHOT_CHANGED"):
            self.store.adopt_legacy_workspace_mode("FIXTURE", "0" * 64)
        self.assertEqual(self.store.count("workspace_identity"), 0)
        self.store.adopt_legacy_workspace_mode("FIXTURE", snapshot["fingerprint"])
        self.assertEqual(self.store.profile("fixture")["email"], PROFILE["email"])
        with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_MISMATCH"):
            self.store.bind_workspace_mode("PERSONAL")

    def test_legacy_adoption_rejects_data_changed_after_preview(self):
        self.store.save_profile(PROFILE)
        snapshot = self.store.workspace_status()
        self.store.save_profile(dict(PROFILE, name="Changed Fixture"))
        with self.assertRaisesRegex(RuntimeError, "WORKSPACE_SNAPSHOT_CHANGED"):
            self.store.adopt_legacy_workspace_mode("FIXTURE", snapshot["fingerprint"])
        self.assertEqual(self.store.count("workspace_identity"), 0)

    def test_legacy_cli_inspects_without_contact_data_and_adopts_exact_snapshot(self):
        self.store.save_profile(PROFILE)
        output = io.StringIO()
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.url), redirect_stdout(output):
            self.assertEqual(workspace_cli(["inspect"]), 0)
        self.assertNotIn(PROFILE["email"], output.getvalue())
        snapshot = json.loads(output.getvalue())
        output = io.StringIO()
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.url), redirect_stdout(output):
            self.assertEqual(workspace_cli(["adopt", "--mode", "FIXTURE", "--expect-fingerprint",
                                            snapshot["fingerprint"], "--acknowledge-reviewed-data"]), 0)
        self.assertEqual(json.loads(output.getvalue())["operation_mode"], "FIXTURE")
        self.assertEqual(self.store.count("career_profiles"), 1)

    def test_api_factory_rejects_legacy_profile_before_serving_reads(self):
        self.store.save_profile(PROFILE)
        with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_UNBOUND"):
            create_app(self.store, "http://127.0.0.1:8090", self.root)
        self.assertEqual(self.store.count("career_profiles"), 1)

    def test_worker_mode_mismatch_does_not_claim_operation(self):
        self.store.bind_workspace_mode("FIXTURE")
        operation = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.url, MJOBS_OPERATION_MODE="PERSONAL"):
            with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_MISMATCH"):
                execute_operation(operation)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.id == operation)).mappings().one()
        self.assertEqual(row["state"], "QUEUED")
        self.assertEqual(row["attempt_count"], 0)

    def test_unbound_legacy_worker_does_not_claim_operation(self):
        operation = enqueue_operation(self.store, "DISCOVER", "fixture", {})
        with patch.dict("os.environ", MJOBS_DATABASE_URL=self.url, MJOBS_OPERATION_MODE="FIXTURE"):
            with self.assertRaisesRegex(RuntimeError, "WORKSPACE_MODE_UNBOUND"):
                execute_operation(operation)
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.operations).where(m.operations.c.id == operation)).mappings().one()
        self.assertEqual(row["state"], "QUEUED")
        self.assertEqual(row["attempt_count"], 0)


if __name__ == "__main__":
    unittest.main()
