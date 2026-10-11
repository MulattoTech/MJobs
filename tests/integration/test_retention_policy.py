"""API, durable policy, and worker acceptance for transient retention settings."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.transient_retention import create_transient_retention_router
from packages.persistence.store import Store
from workers.transient_retention import CONFIG_ENV, run_with_config


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


class RetentionPolicyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-retention-policy-")
        self.root = Path(self.directory.name).resolve()
        self.managed = self.root / "transient" / "screenshots"
        self.managed.mkdir(parents=True)
        self.summary = self.root / ".retention" / "last-sweep.json"
        self.database = self.root / "retention.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.config = {
            "configured_root": str(self.root),
            "allowed_roots": [str(self.root)],
            "rules": [{
                "id": "screenshots",
                "category": "DEBUG_SCREENSHOT",
                "relative_root": "transient/screenshots",
                "pattern": "*.png",
                "max_age_days": 30,
            }],
            "summary_path": str(self.summary),
            # Both persisted user consent and the server's non-dry-run
            # configuration are required before the scheduled worker prunes.
            "dry_run": False,
            "prune_enabled": True,
        }
        self.environment = {CONFIG_ENV: json.dumps(self.config)}
        app = FastAPI()
        app.include_router(create_transient_retention_router(
            store=self.store, environ=self.environment,
        ))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def _write_old(self, path: Path, days: int = 20) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic retention fixture")
        old = (NOW - timedelta(days=days)).timestamp()
        os.utime(path, (old, old))
        return path

    def _put(
        self, *, expected_revision=0, expected_config_fingerprint=None,
        enabled=True, age=15, confirmed=True,
    ):
        if expected_config_fingerprint is None:
            expected_config_fingerprint = self.client.get("/retention/policy").json()["config_fingerprint"]
        return self.client.put("/retention/policy", json={
            "expected_revision": expected_revision,
            "expected_config_fingerprint": expected_config_fingerprint,
            "enabled": enabled,
            "max_age_days_by_rule": {"screenshots": age},
            "confirmed": confirmed,
        })

    def test_policy_api_persists_and_worker_prunes_only_effective_transient_files(self):
        stale = self._write_old(self.managed / "old.png")
        recent = self._write_old(self.managed / "recent.png", days=5)
        unmatched = self._write_old(self.managed / "old.html")
        nested = self._write_old(self.managed / "nested" / "old.png")
        core = self._write_old(self.root / "applications" / "history.json")

        initial = self.client.get("/retention/policy")
        self.assertEqual(initial.status_code, 200, initial.text)
        initial_policy = initial.json()
        self.assertEqual(initial_policy["revision"], 0)
        self.assertFalse(initial_policy["enabled"])
        self.assertTrue(initial_policy["pruning_allowed"])
        self.assertTrue(initial_policy["requires_review"])
        self.assertEqual(initial_policy["rules"], [{
            "id": "screenshots", "category": "DEBUG_SCREENSHOT", "max_age_days": 30,
        }])
        self.assertNotIn(str(self.root), initial.text)
        self.assertEqual(set(initial_policy), {
            "revision", "config_fingerprint", "enabled", "pruning_allowed",
            "rules", "requires_review",
        })

        unconfirmed = self._put(confirmed=1)
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertEqual(unconfirmed.json()["detail"]["code"], "RETENTION_POLICY_INVALID_SETTINGS")
        saved = self._put(
            expected_revision=initial_policy["revision"],
            expected_config_fingerprint=initial_policy["config_fingerprint"], age=15,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        saved_policy = saved.json()
        self.assertEqual(saved_policy["revision"], 1)
        self.assertTrue(saved_policy["enabled"])
        self.assertTrue(saved_policy["pruning_allowed"])
        self.assertEqual(saved_policy["rules"][0]["max_age_days"], 15)

        stale_revision = self._put(
            expected_revision=0,
            expected_config_fingerprint=saved_policy["config_fingerprint"], age=10,
        )
        self.assertEqual(stale_revision.status_code, 409)
        self.assertEqual(stale_revision.json()["detail"]["code"], "RETENTION_POLICY_REVISION_CONFLICT")

        # The API preview remains a read-only scan even after consent is saved.
        preview = self.client.get("/retention/preview")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["preview"]["dry_run"])
        self.assertEqual(preview.json()["preview"]["candidate_count"], 1)
        self.assertEqual(preview.json()["preview"]["removed_count"], 0)
        self.assertEqual(preview.json()["policy"][0]["max_age_days"], 15)
        self.assertTrue(stale.exists())
        self.assertFalse(self.summary.exists())

        # Simulate the scheduled worker's independent Store process/restart.
        self.store.engine.dispose()
        reopened = Store(f"sqlite:///{self.database.as_posix()}")
        try:
            outcome = run_with_config(self.config, store=reopened, now=NOW)
            self.assertEqual(outcome["status"], "OK", outcome)
            self.assertFalse(outcome["dry_run"])
            self.assertEqual(outcome["removed_count"], 1)
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unmatched.exists())
            self.assertTrue(nested.exists())
            self.assertTrue(core.exists())
            self.assertEqual(json.loads(self.summary.read_text(encoding="utf-8")), outcome)
        finally:
            reopened.engine.dispose()

    def test_changed_config_requires_reconfirmation_and_admin_ceiling_blocks_pruning(self):
        stale = self._write_old(self.managed / "must-survive.png")
        initial = self.client.get("/retention/policy").json()
        changed_config = {**self.config, "rules": [{
            **self.config["rules"][0], "pattern": "*.jpg",
        }]}
        self.environment[CONFIG_ENV] = json.dumps(changed_config)
        raced_save = self._put(
            expected_revision=initial["revision"],
            expected_config_fingerprint=initial["config_fingerprint"], enabled=True, age=10,
        )
        self.assertEqual(raced_save.status_code, 409)
        self.assertEqual(raced_save.json()["detail"]["code"], "RETENTION_POLICY_CONFIG_CHANGED")
        self.assertEqual(self.client.get("/retention/policy").json()["revision"], 0)
        self.assertTrue(stale.exists())

        self.environment[CONFIG_ENV] = json.dumps(self.config)
        saved = self._put(
            expected_revision=initial["revision"],
            expected_config_fingerprint=initial["config_fingerprint"], enabled=True, age=10,
        )
        self.assertEqual(saved.status_code, 200, saved.text)

        self.environment[CONFIG_ENV] = json.dumps(changed_config)
        stale_policy = self.client.get("/retention/policy")
        self.assertEqual(stale_policy.status_code, 200, stale_policy.text)
        self.assertTrue(stale_policy.json()["requires_review"])
        self.assertFalse(stale_policy.json()["enabled"])
        self.assertNotEqual(stale_policy.json()["config_fingerprint"], saved.json()["config_fingerprint"])

        self.store.engine.dispose()
        reopened = Store(f"sqlite:///{self.database.as_posix()}")
        try:
            outcome = run_with_config(changed_config, store=reopened, now=NOW)
            self.assertEqual(outcome["status"], "OK", outcome)
            self.assertTrue(outcome["dry_run"])
            self.assertEqual(outcome["removed_count"], 0)
            self.assertTrue(stale.exists(), "changed server policy must invalidate prior consent")
        finally:
            reopened.engine.dispose()

        admin_disabled = {**changed_config, "prune_enabled": False}
        self.environment[CONFIG_ENV] = json.dumps(admin_disabled)
        get_after_admin_change = self.client.get("/retention/policy")
        self.assertFalse(get_after_admin_change.json()["pruning_allowed"])
        current_revision = get_after_admin_change.json()["revision"]
        denied = self._put(expected_revision=current_revision, enabled=True, age=30)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"]["code"], "RETENTION_POLICY_ADMIN_DISABLED")
        disabled_save = self._put(expected_revision=current_revision, enabled=False, age=30)
        self.assertEqual(disabled_save.status_code, 200, disabled_save.text)
        self.assertFalse(disabled_save.json()["enabled"])
        self.assertTrue(stale.exists())


if __name__ == "__main__":
    unittest.main()
