"""Persistent, config-bound consent for bounded transient retention."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from packages.persistence.store import Store
from services.privacy.retention_policy import (
    RetentionPolicyError,
    effective_retention_config,
    read_retention_policy,
    save_retention_policy,
)


class RetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.database = self.directory / "retention.sqlite"
        self.database_url = f"sqlite:///{self.database.as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.addCleanup(self.store.engine.dispose)
        self.root = self.directory / "configured-output"
        self.root.mkdir()
        self.config = self._config()

    def _config(self, *, prune_enabled=True, dry_run=False, pattern="capture-*.png", max_age=30):
        return {
            "configured_root": str(self.root),
            "allowed_roots": [str(self.root)],
            "summary_path": str(self.root / "retention-summary.json"),
            "prune_enabled": prune_enabled,
            "dry_run": dry_run,
            "rules": [{
                "id": "debug-captures",
                "category": "DEBUG_SCREENSHOT",
                "relative_root": "transient/screenshots",
                "pattern": pattern,
                "max_age_days": max_age,
            }, {
                "id": "rotated-logs",
                "category": "ROTATED_LOG",
                "relative_root": "transient/logs",
                "pattern": "mjobs.log.*",
                "max_age_days": 14,
            }],
        }

    def test_missing_policy_defaults_disabled_and_requires_explicit_review(self):
        policy = read_retention_policy(self.store, self.config)

        self.assertEqual(policy["revision"], 0)
        self.assertFalse(policy["enabled"])
        self.assertTrue(policy["pruning_allowed"])
        self.assertTrue(policy["requires_review"])
        self.assertEqual(policy["rules"], [
            {"id": "debug-captures", "category": "DEBUG_SCREENSHOT", "max_age_days": 30},
            {"id": "rotated-logs", "category": "ROTATED_LOG", "max_age_days": 14},
        ])
        self.assertNotIn(str(self.root), repr(policy))

    def test_save_is_atomic_versioned_and_survives_store_reopen(self):
        policy = save_retention_policy(
            self.store, self.config, expected_revision=0,
            expected_config_fingerprint=read_retention_policy(self.store, self.config)["config_fingerprint"], enabled=True,
            max_age_days_by_rule={"debug-captures": 10, "rotated-logs": 365},
        )
        self.store.engine.dispose()
        reopened = Store(self.database_url)
        self.addCleanup(reopened.engine.dispose)

        readback = read_retention_policy(reopened, self.config)

        self.assertEqual(policy, readback)
        self.assertEqual(readback["revision"], 1)
        self.assertTrue(readback["enabled"])
        self.assertFalse(readback["requires_review"])
        self.assertEqual([row["max_age_days"] for row in readback["rules"]], [10, 365])

    def test_config_change_invalidates_consent_and_restores_new_defaults(self):
        saved = save_retention_policy(
            self.store, self.config, expected_revision=0,
            expected_config_fingerprint=read_retention_policy(self.store, self.config)["config_fingerprint"], enabled=True,
            max_age_days_by_rule={"debug-captures": 10, "rotated-logs": 20},
        )
        changed = self._config(pattern="capture-v2-*.png", max_age=45)

        current = read_retention_policy(self.store, changed)
        effective = effective_retention_config(self.store, changed)

        self.assertNotEqual(saved["config_fingerprint"], current["config_fingerprint"])
        self.assertEqual(current["revision"], 1)
        self.assertFalse(current["enabled"])
        self.assertTrue(current["requires_review"])
        self.assertEqual(current["rules"][0]["max_age_days"], 45)
        self.assertEqual(effective["configured_root"], changed["configured_root"])
        self.assertTrue(effective["dry_run"])
        self.assertFalse(effective["prune_enabled"])
        self.assertEqual(effective["rules"][0]["max_age_days"], 45)

    def test_save_rejects_config_changed_since_get_even_when_revision_matches(self):
        seen_at_get = read_retention_policy(self.store, self.config)
        changed = self._config(pattern="capture-v2-*.png", max_age=45)

        with self.assertRaises(RetentionPolicyError) as caught:
            save_retention_policy(
                self.store, changed, expected_revision=seen_at_get["revision"],
                expected_config_fingerprint=seen_at_get["config_fingerprint"], enabled=True,
                max_age_days_by_rule={"debug-captures": 10, "rotated-logs": 14},
            )

        self.assertEqual(caught.exception.code, "RETENTION_POLICY_CONFIG_CHANGED")
        self.assertEqual(read_retention_policy(self.store, changed)["revision"], 0)

    def test_admin_prune_ceiling_requires_literal_true(self):
        disabled = self._config(prune_enabled=False)
        policy = read_retention_policy(self.store, disabled)
        self.assertFalse(policy["pruning_allowed"])
        with self.assertRaises(RetentionPolicyError) as caught:
            save_retention_policy(
                self.store, disabled, expected_revision=0,
                expected_config_fingerprint=policy["config_fingerprint"], enabled=True,
                max_age_days_by_rule={"debug-captures": 30, "rotated-logs": 14},
            )
        self.assertEqual(caught.exception.code, "RETENTION_POLICY_ADMIN_DISABLED")

        invalid_admin = self._config(prune_enabled=1)
        with self.assertRaises(RetentionPolicyError) as invalid:
            read_retention_policy(self.store, invalid_admin)
        self.assertEqual(invalid.exception.code, "RETENTION_POLICY_INVALID_CONFIG")

        admin_dry_run = self._config(dry_run=True)
        dry_policy = read_retention_policy(self.store, admin_dry_run)
        self.assertFalse(dry_policy["pruning_allowed"])
        with self.assertRaises(RetentionPolicyError) as dry_disabled:
            save_retention_policy(
                self.store, admin_dry_run, expected_revision=0,
                expected_config_fingerprint=dry_policy["config_fingerprint"], enabled=True,
                max_age_days_by_rule={"debug-captures": 30, "rotated-logs": 14},
            )
        self.assertEqual(dry_disabled.exception.code, "RETENTION_POLICY_ADMIN_DISABLED")

    def test_config_rejects_traversal_protected_roots_and_broad_patterns(self):
        for update in (
            {"relative_root": "../resumes"},
            {"relative_root": "transient/source"},
            {"relative_root": "transient\\screenshots"},
            {"pattern": "*"},
        ):
            invalid = self._config()
            invalid["rules"] = [dict(self.config["rules"][0], **update), self.config["rules"][1]]
            with self.subTest(update=update), self.assertRaises(RetentionPolicyError) as caught:
                read_retention_policy(self.store, invalid)
            self.assertEqual(caught.exception.code, "RETENTION_POLICY_INVALID_CONFIG")

    def test_runtime_summary_is_ignored_but_execution_flags_invalidate_consent(self):
        saved = save_retention_policy(
            self.store, self.config, expected_revision=0,
            expected_config_fingerprint=read_retention_policy(self.store, self.config)["config_fingerprint"], enabled=True,
            max_age_days_by_rule={"debug-captures": 10, "rotated-logs": 20},
        )
        runtime_only = dict(self.config, dry_run=True,
                            summary_path=str(self.root / "another-summary.json"))

        current = read_retention_policy(self.store, runtime_only)
        effective = effective_retention_config(self.store, runtime_only)

        self.assertNotEqual(current["config_fingerprint"], saved["config_fingerprint"])
        self.assertFalse(current["enabled"])
        self.assertTrue(current["requires_review"])
        self.assertTrue(effective["dry_run"])
        self.assertFalse(effective["prune_enabled"])

        # An unchanged, explicitly consented config can execute only when both
        # the user setting and the server's administrative ceiling allow it.
        authorized = dict(self.config, dry_run=False)
        save_retention_policy(
            self.store, authorized, expected_revision=1,
            expected_config_fingerprint=read_retention_policy(self.store, authorized)["config_fingerprint"], enabled=True,
            max_age_days_by_rule={"debug-captures": 10, "rotated-logs": 20},
        )
        effective = effective_retention_config(self.store, authorized)
        self.assertFalse(effective["dry_run"])
        self.assertTrue(effective["prune_enabled"])

    def test_revision_conflicts_and_exact_rule_age_bounds_are_fixed_code(self):
        save_retention_policy(
            self.store, self.config, expected_revision=0,
            expected_config_fingerprint=read_retention_policy(self.store, self.config)["config_fingerprint"], enabled=False,
            max_age_days_by_rule={"debug-captures": 1, "rotated-logs": 365},
        )
        with self.assertRaises(RetentionPolicyError) as stale:
            save_retention_policy(
                self.store, self.config, expected_revision=0,
                expected_config_fingerprint=read_retention_policy(self.store, self.config)["config_fingerprint"], enabled=False,
                max_age_days_by_rule={"debug-captures": 2, "rotated-logs": 14},
            )
        self.assertEqual(stale.exception.code, "RETENTION_POLICY_REVISION_CONFLICT")

        for ages in (
            {"debug-captures": 0, "rotated-logs": 14},
            {"debug-captures": 366, "rotated-logs": 14},
            {"debug-captures": True, "rotated-logs": 14},
            {"debug-captures": 1, "rotated-logs": 14, "unexpected": 7},
            {"debug-captures": 1},
        ):
            with self.subTest(ages=ages), self.assertRaises(RetentionPolicyError) as invalid:
                save_retention_policy(
                    self.store, self.config, expected_revision=1,
                    expected_config_fingerprint=read_retention_policy(self.store, self.config)["config_fingerprint"], enabled=False,
                    max_age_days_by_rule=ages,
                )
            self.assertEqual(invalid.exception.code, "RETENTION_POLICY_INVALID_SETTINGS")

    def test_concurrent_stale_writes_have_one_compare_and_swap_winner(self):
        def save(age):
            try:
                return save_retention_policy(
                    self.store, self.config, expected_revision=0,
                    expected_config_fingerprint=read_retention_policy(self.store, self.config)["config_fingerprint"], enabled=False,
                    max_age_days_by_rule={"debug-captures": age, "rotated-logs": 14},
                )["revision"]
            except RetentionPolicyError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, (10, 20)))

        self.assertCountEqual(outcomes, [1, "RETENTION_POLICY_REVISION_CONFLICT"])
        self.assertEqual(read_retention_policy(self.store, self.config)["revision"], 1)


if __name__ == "__main__":
    unittest.main()
