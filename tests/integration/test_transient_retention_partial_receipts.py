"""Completed partial cleanups retain their actual outcome across API readback."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.transient_retention import create_transient_retention_router
from packages.persistence.store import Store
from services.privacy.retention_policy import read_retention_policy, save_retention_policy
from services.privacy.transient_retention import run_transient_retention
from workers import transient_retention as worker


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


class PartialRetentionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-partial-retention-")
        self.root = Path(self.directory.name).resolve()
        self.managed = self.root / "transient" / "screenshots"
        self.managed.mkdir(parents=True)
        self.summary_path = self.root / ".retention" / "last-sweep.json"
        self.summary_path.parent.mkdir()
        self.database_url = f"sqlite:///{(self.root / 'policy.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.config = {
            "configured_root": str(self.root),
            "allowed_roots": [str(self.root)],
            "rules": [{
                "id": "debug-screenshots", "category": "DEBUG_SCREENSHOT",
                "relative_root": "transient/screenshots", "pattern": "*.png",
                "max_age_days": 30,
            }],
            "summary_path": str(self.summary_path),
            "dry_run": False,
            "prune_enabled": True,
        }
        initial = read_retention_policy(self.store, self.config)
        save_retention_policy(
            self.store, self.config, expected_revision=initial["revision"],
            expected_config_fingerprint=initial["config_fingerprint"], enabled=True,
            max_age_days_by_rule={"debug-screenshots": 30},
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _write_old(self, name, content):
        path = self.managed / name
        path.write_bytes(content)
        expired = (NOW - timedelta(days=31)).timestamp()
        os.utime(path, (expired, expired))
        return path

    def _partial_sweep(self, **options):
        removed = self._write_old("removed-private-name.png", b"removed bytes")
        retained = self._write_old("retained-private-name.png", b"retained bytes")
        original_unlink = Path.unlink

        def refuse_one_file(path, *args, **kwargs):
            if path == retained:
                raise PermissionError("synthetic private filesystem diagnostic")
            return original_unlink(path, *args, **kwargs)

        # Exercise the actual deletion loop. Only one OS deletion is refused;
        # the other file is really removed regardless of directory order.
        with patch.object(Path, "unlink", autospec=True, side_effect=refuse_one_file):
            result = worker.run_with_config(
                self.config, store=self.store, now=NOW, **options,
            )
        self.assertFalse(removed.exists())
        self.assertTrue(retained.exists())
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["removed_bytes"], len(b"removed bytes"))
        self.assertIn("PRUNE_FAILED", result["errors"])
        serialized = json.dumps(result)
        for private_value in (str(self.root), removed.name, retained.name,
                              "synthetic private filesystem diagnostic"):
            self.assertNotIn(private_value, serialized)
        return result, retained

    def _api_readback(self):
        app = FastAPI()
        app.include_router(create_transient_retention_router(
            store=self.store,
            environ={worker.CONFIG_ENV: json.dumps(self.config)},
        ))
        with TestClient(app) as client:
            response = client.get("/retention/preview")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(str(self.root), response.text)
            return response.json()

    def test_first_partial_receipt_survives_reopen_readback_and_retry(self):
        result, retained = self._partial_sweep()
        self.assertEqual(worker.read_saved_summary(self.config), result)
        self.assertEqual(json.loads(self.summary_path.read_text(encoding="utf-8")), result)

        # A fresh Store and router must read the completed partial result from
        # disk, while GET preview remains read-only and keeps that receipt.
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        readback = self._api_readback()
        self.assertEqual(readback["last_sweep"], result)
        self.assertTrue(readback["preview"]["dry_run"])
        self.assertEqual(readback["preview"]["removed_count"], 0)
        self.assertTrue(retained.exists())
        self.assertEqual(worker.read_saved_summary(self.config), result)

        recovered = worker.run_with_config(self.config, store=self.store, now=NOW)
        self.assertEqual(recovered["status"], "OK")
        self.assertEqual(recovered["candidate_count"], 1)
        self.assertEqual(recovered["removed_count"], 1)
        self.assertEqual(recovered["removed_bytes"], len(b"retained bytes"))
        self.assertFalse(retained.exists())
        self.assertEqual(self._api_readback()["last_sweep"], recovered)

    def test_partial_receipt_replaces_stale_success_receipt(self):
        previous = worker.run_with_config(self.config, store=self.store, now=NOW)
        self.assertEqual(previous["status"], "OK")
        self.assertEqual(previous["removed_count"], 0)
        self.assertEqual(worker.read_saved_summary(self.config), previous)

        result, _retained = self._partial_sweep()
        self.assertEqual(worker.read_saved_summary(self.config), result)
        self.assertEqual(self._api_readback()["last_sweep"], result)

    def test_partial_receipt_write_failure_keeps_counts_and_previous_receipt(self):
        previous = worker.run_with_config(self.config, store=self.store, now=NOW)
        previous_bytes = self.summary_path.read_bytes()
        with patch.object(worker, "_write_summary", side_effect=OSError("private receipt failure")):
            result, _retained = self._partial_sweep()

        self.assertEqual(result["errors"], ["PRUNE_FAILED", "SUMMARY_WRITE_FAILED"])
        self.assertNotIn("private receipt failure", json.dumps(result))
        self.assertEqual(self.summary_path.read_bytes(), previous_bytes)
        self.assertEqual(worker.read_saved_summary(self.config), previous)

    def test_partial_sweep_preserves_unknown_destination_created_during_cleanup(self):
        unrelated = b"synthetic unrelated history must survive"

        def sweep_and_create_destination(*args, **kwargs):
            result = run_transient_retention(*args, **kwargs)
            self.summary_path.write_bytes(unrelated)
            return result

        result, _retained = self._partial_sweep(engine=sweep_and_create_destination)
        self.assertEqual(result["errors"], ["PRUNE_FAILED", "SUMMARY_PATH_UNSAFE"])
        self.assertEqual(self.summary_path.read_bytes(), unrelated)
        self.assertIsNone(worker.read_saved_summary(self.config))
