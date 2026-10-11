"""Bounded and allowlisted transient-file retention."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.privacy.transient_retention import run_transient_retention


class TransientRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "configured-output"
        self.root.mkdir()
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        self.rules = [{"id": "old-raw-payloads", "category": "RAW_PAYLOAD",
                       "relative_root": "transient/raw", "pattern": "raw-*.json",
                       "max_age_days": 5}]

    def _file(self, name, *, age_days=6, data=b"synthetic transient payload"):
        directory = self.root / "transient" / "raw"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        modified = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(path, (modified, modified))
        return path

    def test_dry_run_reports_old_file_counts_and_bytes_without_deleting(self):
        path = self._file("raw-001.json")

        result = run_transient_retention(
            self.root, self.rules, allowed_roots=[self.root], now=self.now,
        )

        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["candidate_bytes"], path.stat().st_size)
        self.assertEqual(result["removed_count"], 0)
        self.assertEqual(result["by_rule"][0]["id"], "old-raw-payloads")
        self.assertEqual(result["by_category"]["RAW_PAYLOAD"]["candidate_count"], 1)
        self.assertTrue(path.exists())
        self.assertNotIn("raw-001.json", repr(result))
        self.assertNotIn("synthetic transient payload", repr(result))

    def test_explicit_prune_removes_only_expired_allowlisted_file(self):
        expired = self._file("raw-001.json")
        fresh = self._file("raw-002.json", age_days=4)

        result = run_transient_retention(
            self.root, self.rules, allowed_roots=[self.root], now=self.now,
            dry_run=False,
        )

        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["removed_bytes"], len(b"synthetic transient payload"))
        self.assertEqual(result["by_category"]["RAW_PAYLOAD"]["removed_count"], 1)
        self.assertEqual(result["by_rule"][0]["removed_bytes"], len(b"synthetic transient payload"))
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.exists())

    def test_exact_age_boundary_is_retained(self):
        boundary = self._file("raw-boundary.json", age_days=5)

        result = run_transient_retention(
            self.root, self.rules, allowed_roots=[self.root], now=self.now,
            dry_run=False,
        )

        self.assertEqual(result["candidate_count"], 0)
        self.assertTrue(boundary.exists())

    def test_unrecognized_names_and_nested_files_are_preserved(self):
        bad_name = self._file("unmanaged-001.json")
        nested = self.root / "transient" / "raw" / "nested"
        nested.mkdir()
        nested_file = nested / "raw-002.json"
        nested_file.write_bytes(b"do not recurse")
        old = self.now - timedelta(days=6)
        os.utime(nested_file, (old.timestamp(), old.timestamp()))

        result = run_transient_retention(
            self.root, self.rules, allowed_roots=[self.root], now=self.now,
            dry_run=False,
        )

        self.assertEqual(result["removed_count"], 0)
        self.assertTrue(bad_name.exists())
        self.assertTrue(nested_file.exists())

    def test_root_outside_allowlist_fails_closed_with_fixed_error(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        candidate = other / "raw-001.json"
        candidate.write_bytes(b"protected")

        result = run_transient_retention(
            other, self.rules, allowed_roots=[self.root], now=self.now,
            dry_run=False,
        )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["errors"], ["ROOT_NOT_ALLOWLISTED"])
        self.assertTrue(candidate.exists())
        self.assertNotIn(str(other), repr(result))

    def test_traversal_symlink_and_invalid_age_are_rejected_without_pruning(self):
        protected = self._file("raw-001.json")
        bad_rules = [
            [{**self.rules[0], "relative_root": "../transient/raw"}],
            [{**self.rules[0], "pattern": "../*.json"}],
            [{**self.rules[0], "max_age_days": 0}],
            [{**self.rules[0], "relative_root": "applications/history"}],
            [{**self.rules[0], "category": "UNKNOWN"}],
            [{**self.rules[0], "id": "../unsafe"}],
        ]
        for rules in bad_rules:
            with self.subTest(rules=rules):
                result = run_transient_retention(
                    self.root, rules, allowed_roots=[self.root], now=self.now,
                    dry_run=False,
                )
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["removed_count"], 0)
                self.assertTrue(protected.exists())

    def test_symlinked_managed_root_is_not_followed(self):
        target = self.root / "protected"
        target.mkdir()
        outside = target / "raw-001.json"
        outside.write_bytes(b"preserve")
        managed_parent = self.root / "transient"
        managed_parent.mkdir()
        managed_path = managed_parent / "raw"
        managed_path.mkdir()
        original_is_symlink = Path.is_symlink

        def report_managed_path_as_symlink(path):
            return path == managed_path or original_is_symlink(path)

        with patch.object(Path, "is_symlink", report_managed_path_as_symlink):
            result = run_transient_retention(
                self.root, self.rules, allowed_roots=[self.root], now=self.now,
                dry_run=False,
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("UNSAFE_PATH", result["errors"])
        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
