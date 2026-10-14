"""Retention receipts cannot replace core data or hide a completed deletion."""
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.transient_retention import create_transient_retention_router
from tests.integration import test_transient_retention as fixtures
from workers import transient_retention as worker


class RetentionReceiptSafetyTests(unittest.TestCase):
    # Reuse the real isolated SQLite/filesystem fixture, not its test methods.
    setUp = fixtures.TransientRetentionIntegrationTests.setUp
    tearDown = fixtures.TransientRetentionIntegrationTests.tearDown
    _write_old = fixtures.TransientRetentionIntegrationTests._write_old
    _engine = fixtures.TransientRetentionIntegrationTests._engine
    def test_core_history_destination_blocks_before_pruning(self):
        old = self._write_old("expired.png")
        protected = self.root / "applications" / "history.json"
        protected.parent.mkdir()
        protected.write_bytes(b'{"application_history":"preserve"}')
        result = worker.run_with_config({**self.config, "dry_run": False,
            "prune_enabled": True, "summary_path": str(protected)}, engine=self._engine)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["removed_count"], 0)
        self.assertTrue(old.exists())
        self.assertEqual(protected.read_bytes(), b'{"application_history":"preserve"}')

    def test_unrecognized_existing_destination_blocks_before_pruning(self):
        old = self._write_old("expired.png")
        for payload in (b'private synthetic history', b'{"status":"OK","dry_run":false}'):
            with self.subTest(payload=payload):
                self.summary_path.write_bytes(payload)
                result = worker.run_with_config({**self.config, "dry_run": False,
                    "prune_enabled": True}, engine=self._engine)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["errors"], ["SUMMARY_PATH_UNSAFE"])
                self.assertTrue(old.exists())
                self.assertEqual(self.summary_path.read_bytes(), payload)
                self.assertIsNone(worker.read_saved_summary(self.config))

    def test_write_failure_keeps_actual_removals_and_retry_readback(self):
        old = self._write_old("expired.png", b"removed bytes")
        live = {**self.config, "dry_run": False, "prune_enabled": True}
        with patch.object(worker, "_write_summary", side_effect=OSError("private diagnostic")):
            result = worker.run_with_config(live, engine=self._engine)
        self.assertFalse(old.exists())
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["removed_bytes"], 13)
        self.assertEqual(result["errors"], ["SUMMARY_WRITE_FAILED"])
        self.assertNotIn("private diagnostic", json.dumps(result))
        self.assertFalse(self.summary_path.exists())
        recovered = worker.run_with_config(live, engine=self._engine)
        self.assertEqual(recovered["status"], "OK")
        self.assertEqual(recovered["removed_count"], 0)
        app = FastAPI()
        app.include_router(create_transient_retention_router(
            store=self.store, environ={worker.CONFIG_ENV: json.dumps(live)}, engine=self._engine))
        with TestClient(app) as client:
            saved = client.get("/retention/preview").json()["last_sweep"]
        self.assertEqual(saved, recovered)
        self.assertNotIn(str(self.root), json.dumps(saved))

    def test_new_unknown_destination_after_sweep_preserves_file_and_counts(self):
        old = self._write_old("expired.png", b"removed bytes")
        def sweep(*args, **kwargs):
            result = self._engine(*args, **kwargs)
            self.summary_path.write_bytes(b"new unrelated content")
            return result
        result = worker.run_with_config({**self.config, "dry_run": False,
            "prune_enabled": True}, engine=sweep)
        self.assertFalse(old.exists())
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["errors"], ["SUMMARY_PATH_UNSAFE"])
        self.assertEqual(self.summary_path.read_bytes(), b"new unrelated content")
