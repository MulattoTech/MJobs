"""Truthful cleanup disclosure at the mounted, read-only preview boundary."""
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.transient_retention import create_transient_retention_router
from packages.persistence import models as m
from packages.persistence.store import Store
from workers.transient_retention import CONFIG_ENV, run_with_config


class RetentionTransparencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-retention-disclosure-")
        self.root = Path(self.directory.name).resolve()
        self.managed = self.root / "transient" / "screenshots"
        self.managed.mkdir(parents=True)
        self.artifact = self.managed / "private-synthetic-name.png"
        self.artifact.write_bytes(b"private synthetic screenshot")
        old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
        os.utime(self.artifact, (old, old))
        self.receipt = self.root / ".retention" / "last-sweep.json"
        self.database_url = f"sqlite:///{(self.root / 'retention.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.config = {
            "configured_root": str(self.root), "allowed_roots": [str(self.root)],
            "summary_path": str(self.receipt), "dry_run": False, "prune_enabled": True,
            "rules": [{"id": "screenshots", "category": "DEBUG_SCREENSHOT",
                       "relative_root": "transient/screenshots", "pattern": "*.png",
                       "max_age_days": 30}],
        }
        self.environment = {CONFIG_ENV: json.dumps(self.config)}
        self.client = self._client(self.store)

    def _client(self, store):
        app = FastAPI()
        app.include_router(create_transient_retention_router(
            store=store, environ=self.environment,
        ))
        return TestClient(app)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _save(self, enabled):
        current = self.client.get("/retention/policy").json()
        response = self.client.put("/retention/policy", json={
            "expected_revision": current["revision"],
            "expected_config_fingerprint": current["config_fingerprint"],
            "enabled": enabled, "max_age_days_by_rule": {"screenshots": 30},
            "confirmed": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _preview(self, client=None):
        response = (client or self.client).get("/retention/preview")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn(self.artifact.name, response.text)
        self.assertNotIn("private synthetic screenshot", response.text)
        payload = response.json()
        self.assertTrue(payload["preview"]["dry_run"])
        self.assertEqual(payload["preview"]["removed_count"], 0)
        self.assertEqual(set(payload["cleanup_control"]), {
            "source", "state", "policy_revision",
        })
        return payload

    def test_configured_rule_without_saved_consent_requires_review_and_writes_nothing(self):
        with self.store.engine.connect() as connection:
            before = connection.execute(select(m.audit)).all()
        result = self._preview()
        self.assertEqual(result["cleanup_control"], {
            "source": "PERSISTED_POLICY", "state": "REVIEW_REQUIRED", "policy_revision": 0,
        })
        self.assertEqual(result["preview"]["candidate_count"], 1)
        self.assertEqual(len(result["policy"]), 1)
        self.assertTrue(self.artifact.exists())
        self.assertFalse(self.receipt.exists())
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.audit)).all(), before)

    def test_saved_disabled_policy_is_not_inferred_enabled_from_rule_or_candidates(self):
        self._save(False)
        result = self._preview()
        self.assertEqual(result["cleanup_control"], {
            "source": "PERSISTED_POLICY", "state": "DISABLED", "policy_revision": 1,
        })
        self.assertEqual(result["preview"]["candidate_count"], 1)
        self.assertTrue(run_with_config(self.config, store=self.store)["dry_run"])
        self.assertTrue(self.artifact.exists())

    def test_allowed_policy_survives_reopen_and_read_only_preview_override(self):
        self._save(True)
        expected = {"source": "PERSISTED_POLICY", "state": "ALLOWED", "policy_revision": 1}
        self.assertEqual(self._preview()["cleanup_control"], expected)
        self.store.engine.dispose()
        reopened = Store(self.database_url)
        try:
            with self._client(reopened) as client:
                self.assertEqual(self._preview(client)["cleanup_control"], expected)
        finally:
            reopened.engine.dispose()
        self.assertTrue(self.artifact.exists(), "preview must not perform an allowed cleanup")
        self.assertFalse(self.receipt.exists())

    def test_server_ceiling_takes_precedence_over_stale_or_missing_consent(self):
        self._save(True)
        for dry_run, prune_enabled in ((True, True), (False, False), (True, False)):
            with self.subTest(dry_run=dry_run, prune_enabled=prune_enabled):
                self.environment[CONFIG_ENV] = json.dumps({
                    **self.config, "dry_run": dry_run, "prune_enabled": prune_enabled,
                })
                self.assertEqual(self._preview()["cleanup_control"], {
                    "source": "PERSISTED_POLICY", "state": "SERVER_DISABLED", "policy_revision": 1,
                })
        self.assertTrue(self.artifact.exists())

    def test_changed_configuration_requires_review_despite_positive_saved_revision(self):
        self._save(True)
        changed = {**self.config, "rules": [{**self.config["rules"][0], "max_age_days": 10}]}
        self.environment[CONFIG_ENV] = json.dumps(changed)
        result = self._preview()
        self.assertEqual(result["cleanup_control"], {
            "source": "PERSISTED_POLICY", "state": "REVIEW_REQUIRED", "policy_revision": 1,
        })
        self.assertTrue(self.artifact.exists())

    def test_corrupt_consent_is_disclosed_as_needing_review_without_echoing_payload(self):
        self._save(True)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(
                m.audit.c.entity_id == "SYSTEM_TRANSIENT_RETENTION",
            ).values(payload={"revision": 1, "enabled": "private-secret-sentinel"}))
        result = self._preview()
        self.assertEqual(result["cleanup_control"]["state"], "REVIEW_REQUIRED")
        self.assertEqual(result["cleanup_control"]["policy_revision"], 1)
        self.assertNotIn("private-secret-sentinel", json.dumps(result))
        self.assertTrue(self.artifact.exists())

    def test_without_store_reports_server_configuration_and_never_invents_saved_consent(self):
        with self._client(None) as client:
            for dry_run, prune_enabled, expected in (
                (False, True, "ALLOWED"), (True, True, "SERVER_DISABLED"),
                (False, False, "SERVER_DISABLED"), (True, False, "SERVER_DISABLED"),
            ):
                with self.subTest(dry_run=dry_run, prune_enabled=prune_enabled):
                    self.environment[CONFIG_ENV] = json.dumps({
                        **self.config, "dry_run": dry_run, "prune_enabled": prune_enabled,
                    })
                    self.assertEqual(self._preview(client)["cleanup_control"], {
                        "source": "SERVER_CONFIGURATION", "state": expected, "policy_revision": None,
                    })
        self.assertTrue(self.artifact.exists())
        self.assertFalse(self.receipt.exists())

    def test_allowed_configuration_does_not_turn_an_unsafe_scan_into_a_success(self):
        self._save(True)
        linked = self.managed / "linked.png"
        try:
            linked.symlink_to(self.artifact)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        result = self._preview()
        self.assertEqual(result["cleanup_control"]["state"], "ALLOWED")
        self.assertEqual(result["preview"]["status"], "BLOCKED")
        self.assertTrue(self.artifact.exists())
        self.assertFalse(self.receipt.exists())

    def test_historical_success_does_not_override_current_disabled_policy(self):
        self._save(True)
        sweep = run_with_config(self.config, store=self.store)
        self.assertEqual(sweep["status"], "OK")
        self.assertFalse(sweep["dry_run"])
        self.assertEqual(sweep["removed_count"], 1)
        self._save(False)
        result = self._preview()
        self.assertEqual(result["cleanup_control"], {
            "source": "PERSISTED_POLICY", "state": "DISABLED", "policy_revision": 2,
        })
        self.assertEqual(result["last_sweep"], sweep)

    def test_invalid_config_returns_safe_error_instead_of_a_cleanup_permission(self):
        self.environment[CONFIG_ENV] = "private-config-sentinel"
        response = self.client.get("/retention/preview")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": {"code": "TRANSIENT_RETENTION_CONFIG_INVALID"}})
        self.assertNotIn("private-config-sentinel", response.text)
        self.assertTrue(self.artifact.exists())


if __name__ == "__main__":
    unittest.main()
