"""Runtime integration for configured, fail-closed transient retention."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.transient_retention import create_transient_retention_router
from packages.persistence.store import Store
from services.privacy.transient_retention import run_transient_retention
from services.privacy import transient_retention
from workers.transient_retention import CONFIG_ENV, run_from_environment, run_with_config


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


class TransientRetentionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-retention-runtime-")
        self.root = Path(self.directory.name).resolve()
        self.managed = self.root / "transient" / "screenshots"
        self.managed.mkdir(parents=True)
        self.summary_root = self.root / ".retention"
        self.summary_root.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.summary_path = self.summary_root / "last-sweep.json"
        self.config = {
            "configured_root": str(self.root),
            "allowed_roots": [str(self.root)],
            "rules": [{
                "id": "debug-screenshots",
                "category": "DEBUG_SCREENSHOT",
                "relative_root": "transient/screenshots",
                "pattern": "*.png",
                "max_age_days": 30,
            }],
            "summary_path": str(self.summary_path),
            "dry_run": True,
            "prune_enabled": False,
        }
        self.store = Store(f"sqlite:///{(self.root / 'api.db').as_posix()}")
        self.store.migrate()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _write_old(self, name: str, content: bytes = b"synthetic transient") -> Path:
        path = self.managed / name
        path.write_bytes(content)
        old = (NOW - timedelta(days=31)).timestamp()
        os.utime(path, (old, old))
        return path

    def _engine(self, configured_root, rules, *, allowed_roots, now=None, dry_run=True):
        return run_transient_retention(
            configured_root, rules, allowed_roots=allowed_roots,
            now=NOW, dry_run=dry_run,
        )

    def test_worker_dry_run_prune_repeat_and_interruption_recovery(self):
        old = self._write_old("old-debug.png", b"synthetic old capture")
        second = self._write_old("second-debug.png", b"second capture")
        boundary = self.managed / "at-boundary.png"
        boundary.write_bytes(b"exactly at retention boundary")
        cutoff = (NOW - timedelta(days=30)).timestamp()
        os.utime(boundary, (cutoff, cutoff))
        unrelated = self.root / "applications"
        unrelated.mkdir()
        core = unrelated / "history.png"
        core.write_bytes(b"core application history")
        nested = self.managed / "nested"
        nested.mkdir()
        nested_old = nested / "nested-old.png"
        nested_old.write_bytes(b"nested file is outside direct-child policy")
        os.utime(nested_old, (old.stat().st_mtime, old.stat().st_mtime))

        dry_run = run_with_config(self.config, engine=self._engine)
        self.assertEqual(dry_run["status"], "OK")
        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["candidate_count"], 2)
        self.assertEqual(dry_run["removed_count"], 0)
        self.assertEqual(dry_run["by_category"]["DEBUG_SCREENSHOT"]["candidate_count"], 2)
        self.assertTrue(old.exists())
        self.assertTrue(second.exists())
        self.assertFalse(self.summary_path.exists(), "preview must not publish a sweep receipt")

        disabled_prune = run_with_config(
            {**self.config, "dry_run": False}, engine=self._engine,
        )
        self.assertTrue(disabled_prune["dry_run"], "pruning stays disabled until explicitly enabled")
        self.assertEqual(disabled_prune["removed_count"], 0)
        self.assertTrue(old.exists())

        # A process interruption after one completed unlink leaves a consistent
        # subset. The next run is idempotent and completes only remaining files.
        original_unlink = Path.unlink
        unlinks = {"count": 0}

        def interrupt_after_first(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            if path in {old, second}:
                unlinks["count"] += 1
                if unlinks["count"] == 1:
                    raise KeyboardInterrupt

        live_config = {**self.config, "dry_run": False, "prune_enabled": True}
        with patch("pathlib.Path.unlink", autospec=True, side_effect=interrupt_after_first):
            with self.assertRaises(KeyboardInterrupt):
                run_with_config(live_config, engine=self._engine)
        self.assertEqual(unlinks["count"], 1)
        self.assertNotEqual(old.exists(), second.exists())
        self.assertFalse(self.summary_path.exists(), "interrupted sweep must not publish a success receipt")

        completed = run_with_config(live_config, engine=self._engine)
        self.assertEqual(completed["status"], "OK")
        self.assertFalse(completed["dry_run"])
        self.assertEqual(completed["removed_count"], 1)
        self.assertFalse(old.exists())
        self.assertFalse(second.exists())
        self.assertTrue(boundary.exists(), "the exact age boundary is retained")
        self.assertTrue(core.exists(), "application history is never in a configured transient root")
        self.assertTrue(nested_old.exists(), "matching files below direct children are not considered")
        self.assertEqual(json.loads(self.summary_path.read_text(encoding="utf-8")), completed)

        repeated = run_with_config(live_config, engine=self._engine)
        self.assertEqual(repeated["status"], "OK")
        self.assertEqual(repeated["candidate_count"], 0)
        self.assertEqual(repeated["removed_count"], 0)
        self.assertTrue(boundary.exists())
        self.assertTrue(core.exists())

    def test_malformed_root_and_symlink_fail_closed_without_following_target(self):
        old = self._write_old("preserve.png")
        outside_dir = self.root / "outside"
        outside_dir.mkdir()
        outside = outside_dir / "outside.png"
        outside.write_bytes(b"outside the configured root")
        link = self.managed / "linked.png"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            # A directory junction is also a Windows reparse point and is
            # available to non-elevated test processes on supported systems.
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside_dir)],
                capture_output=True, text=True, check=False,
            )
            if result.returncode:
                self.skipTest("symbolic link and junction creation are unavailable")

        blocked = run_with_config(
            {**self.config, "dry_run": False, "prune_enabled": True}, engine=self._engine,
        )
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("UNSAFE_PATH", blocked["errors"])
        self.assertTrue(old.exists())
        self.assertTrue(link.is_symlink() or link.is_junction())
        self.assertEqual(outside.read_bytes(), b"outside the configured root")

        malformed = {**self.config, "rules": [{
            **self.config["rules"][0], "relative_root": "../applications",
        }], "dry_run": False, "prune_enabled": True}
        rejected = run_with_config(malformed, engine=self._engine)
        self.assertEqual(rejected["status"], "BLOCKED")
        self.assertIn("INVALID_RULE", rejected["errors"])
        self.assertTrue(old.exists())
        self.assertTrue(outside.exists())

    def test_worker_entrypoint_reports_only_sanitized_summary(self):
        candidate = self._write_old("private-name-debug.png", b"private synthetic bytes")
        output = StringIO()
        status = run_from_environment(
            {CONFIG_ENV: json.dumps(self.config)}, output=output,
        )
        self.assertEqual(status, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], "OK")
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["candidate_count"], 1)
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertNotIn(candidate.name, output.getvalue())
        self.assertTrue(candidate.exists(), "worker defaults to a read-only sweep")

        missing_output = StringIO()
        missing_status = run_from_environment({}, output=missing_output)
        missing_summary = json.loads(missing_output.getvalue())
        self.assertEqual(missing_status, 1)
        self.assertEqual(missing_summary["status"], "BLOCKED")
        self.assertEqual(missing_summary["errors"], ["CONFIG_NOT_CONFIGURED"])

    def test_api_preview_is_read_only_fixed_scope_and_returns_sanitized_last_sweep(self):
        old = self._write_old("api-preview.png")
        with patch.dict(os.environ, {
                "MJOBS_TRANSIENT_RETENTION_CONFIG": json.dumps(self.config)}):
            with TestClient(create_app(
                    self.store, "http://127.0.0.1:8090", self.output,
                    operation_mode="PERSONAL")) as client:
                response = client.get("/retention/preview", params={"root": str(self.root.parent)})
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["preview"]["status"], "OK")
                self.assertTrue(payload["preview"]["dry_run"])
                self.assertEqual(payload["preview"]["candidate_count"], 1)
                self.assertIsNone(payload["last_sweep"])
                self.assertEqual(payload["policy"][0]["category"], "DEBUG_SCREENSHOT")
                self.assertNotIn(str(self.root), response.text)
                self.assertTrue(old.exists(), "GET preview must not prune")

                run_with_config(
                    {**self.config, "dry_run": False, "prune_enabled": True}, engine=self._engine,
                )
                after_sweep = client.get("/retention/preview")
                self.assertEqual(after_sweep.status_code, 200, after_sweep.text)
                saved = after_sweep.json()["last_sweep"]
                self.assertEqual(saved["status"], "OK")
                self.assertFalse(saved["dry_run"])
                self.assertEqual(saved["removed_count"], 1)
                self.assertFalse(old.exists())
                self.assertNotIn(str(self.root), after_sweep.text)

    def test_worker_redirect_failure_has_no_success_receipt_and_recovers(self):
        old = self._write_old("redirected-debug.png")
        live_config = {**self.config, "dry_run": False, "prune_enabled": True}
        original_reparse = transient_retention._is_reparse_point
        candidate_checks = 0
        redirected = False

        def redirected_ancestor_after_scan(path):
            nonlocal candidate_checks, redirected
            if path == old:
                candidate_checks += 1
                redirected = candidate_checks >= 2
            if path == self.managed and redirected:
                return True
            return original_reparse(path)

        with patch.object(transient_retention, "_is_reparse_point", redirected_ancestor_after_scan):
            blocked = run_with_config(live_config, engine=self._engine)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["errors"], ["UNSAFE_PATH"])
        self.assertEqual(blocked["removed_count"], 0)
        self.assertTrue(old.exists())
        self.assertFalse(self.summary_path.exists(), "blocked pruning cannot publish a success receipt")

        recovered = run_with_config(live_config, engine=self._engine)
        self.assertEqual(recovered["status"], "OK")
        self.assertEqual(recovered["removed_count"], 1)
        self.assertFalse(old.exists())
        app = FastAPI()
        app.include_router(create_transient_retention_router(
            environ={CONFIG_ENV: json.dumps(live_config)}, engine=self._engine,
        ))
        with TestClient(app) as client:
            response = client.get("/retention/preview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["last_sweep"], recovered)
        self.assertNotIn(str(self.root), response.text)

    def test_api_router_can_be_mounted_without_exposing_root_selection(self):
        old = self._write_old("router-preview.png")
        app = FastAPI()
        app.include_router(create_transient_retention_router(engine=self._engine))
        with patch.dict(os.environ, {
                "MJOBS_TRANSIENT_RETENTION_CONFIG": json.dumps(self.config)}):
            with TestClient(app) as client:
                response = client.get("/retention/preview", params={"root": str(self.root.parent)})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["preview"]["candidate_count"], 1)
        self.assertTrue(payload["preview"]["dry_run"])
        self.assertIsNone(payload["last_sweep"])
        self.assertTrue(old.exists())
        self.assertNotIn(str(self.root), response.text)


if __name__ == "__main__":
    unittest.main()
