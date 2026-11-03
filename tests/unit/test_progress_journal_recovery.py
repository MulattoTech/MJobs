"""Explicit sync repairs a derived checkpoint after an interrupted canonical write."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.verification import progress


class ProgressJournalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-journal-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "planning").mkdir()
        (self.root / "docs/evidence").mkdir(parents=True)
        self.evidence = self.root / "docs/evidence/checkpoint.json"
        self.evidence.write_text("{}", encoding="utf-8")
        self.state = self.root / "planning/IMPLEMENTATION_STATE.json"
        self.state.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        self.output = self.root / progress.DEFAULT_OUTPUT_JOURNAL
        self.entry = {
            "milestone_id": "CHECKPOINT-1",
            "timestamp": "2026-11-03T00:00:00Z",
            "current_phase": "FIRST",
            "completed_steps": ["Verified owned fixture"],
            "files_added": ["docs/evidence/checkpoint.json"],
            "files_modified": ["planning/IMPLEMENTATION_STATE.json"],
            "tests_completed": ["Owned subprocess checkpoint"],
            "tests_remaining": ["Physical host restart"],
            "migrations_applied": [],
            "live_integrations_tested": [],
            "known_errors": [],
            "known_limitations": ["Synthetic isolated evidence only"],
            "last_safe_checkpoint": {
                "git_commit": "a" * 40,
                "evidence_path": "docs/evidence/checkpoint.json",
            },
        }
        package = self.root / "tools/verification"
        package.mkdir(parents=True)
        (self.root / "tools/__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "progress.py").write_bytes(Path(progress.__file__).read_bytes())
        self.record(self.entry)

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", "-m", "tools.verification.progress",
             *map(str, arguments)],
            cwd=self.root, capture_output=True, text=True, check=False, timeout=10,
        )

    def record(self, entry):
        path = self.root / "docs/evidence/entry.json"
        path.write_text(json.dumps(entry), encoding="utf-8")
        completed = self.cli("record", "--entry", path)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def interrupt_after_canonical_replace(self):
        next_entry = {
            **self.entry, "milestone_id": "CHECKPOINT-2",
            "timestamp": "2026-11-03T00:01:00Z", "current_phase": "SECOND",
            "completed_steps": ["New checkpoint committed before output publication"],
        }
        script = """
import json, os, sys
from pathlib import Path
from tools.verification import progress
state = Path(sys.argv[1])
replace = progress.os.replace
def replace_and_exit(source, target):
    replace(source, target)
    if Path(target) == state:
        os._exit(73)
progress.os.replace = replace_and_exit
progress.append_milestone(state, json.loads(sys.argv[2]))
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.state), json.dumps(next_entry)],
            cwd=self.root, capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(completed.returncode, 73, completed.stderr)
        canonical = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(canonical["milestone_journal"], [self.entry, next_entry])
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["milestone_id"],
                         "CHECKPOINT-1")
        return next_entry

    def assert_current_output(self):
        canonical = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8")),
            progress.extract_machine_readable_state(canonical["milestone_journal"][-1],
                                                    canonical),
        )
        self.assertEqual(progress.validate_journal(self.root), [])

    def test_interrupted_append_is_repaired_by_fresh_sync_then_resumed_without_duplicate(self):
        next_entry = self.interrupt_after_canonical_replace()
        before = self.state.read_bytes(), self.output.read_bytes()
        invalid = self.cli("validate")
        self.assertEqual(invalid.returncode, 1, invalid.stderr)
        self.assertIn("disagrees with latest milestone", invalid.stdout)
        refused_resume = self.cli("resume")
        self.assertNotEqual(refused_resume.returncode, 0)
        self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)

        repaired = self.cli("sync")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(self.state.read_bytes(), before[0])
        self.assert_current_output()
        resumed = self.cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["latest_milestone"], next_entry)

        final_entry = {
            **next_entry, "milestone_id": "CHECKPOINT-3",
            "timestamp": "2026-11-03T00:02:00Z", "current_phase": "RESUMED",
        }
        self.record(final_entry)
        self.assertEqual(
            [row["milestone_id"] for row in
             json.loads(self.state.read_text(encoding="utf-8"))["milestone_journal"]],
            ["CHECKPOINT-1", "CHECKPOINT-2", "CHECKPOINT-3"],
        )
        self.assert_current_output()

    def test_validation_checks_every_authoritative_field_and_checkpoint_identity(self):
        original = json.loads(self.output.read_text(encoding="utf-8"))
        for field in (*progress.AUTHORITATIVE_FIELDS, "milestone_id", "timestamp",
                      "schema_version"):
            with self.subTest(field=field):
                changed = dict(original)
                if field in progress.LIST_FIELDS:
                    changed[field] = [*original[field], "STALE_OUTPUT_SENTINEL"]
                elif field == "last_safe_checkpoint":
                    changed[field] = {**original[field], "git_commit": "b" * 40}
                elif field == "schema_version":
                    changed[field] = 2
                elif field == "timestamp":
                    changed[field] = "2026-11-03T00:00:01Z"
                else:
                    changed[field] = "STALE_OUTPUT_SENTINEL"
                self.output.write_text(json.dumps(changed), encoding="utf-8")
                before = self.state.read_bytes(), self.output.read_bytes()
                self.assertIn(f"Outputs journal {field} disagrees with latest milestone",
                              progress.validate_journal(self.root))
                self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)

    def test_sync_replaces_corrupt_derived_json_without_changing_canonical_bytes(self):
        self.output.write_text("{truncated", encoding="utf-8")
        before = self.state.read_bytes()
        self.assertTrue(progress.validate_journal(self.root))
        repaired = self.cli("sync")
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(self.state.read_bytes(), before)
        self.assert_current_output()

    def test_sync_refuses_invalid_canonical_even_with_valid_derived_output(self):
        canonical = self.state.read_bytes()
        for defect in ("invalid_json", "missing_evidence", "phase_mismatch"):
            with self.subTest(defect=defect):
                self.state.write_bytes(canonical)
                self.evidence.write_text("{}", encoding="utf-8")
                if defect == "invalid_json":
                    self.state.write_text("{truncated", encoding="utf-8")
                elif defect == "missing_evidence":
                    self.evidence.unlink()
                else:
                    changed = json.loads(canonical)
                    changed["current_phase"] = "NOT_THE_COMMITTED_PHASE"
                    self.state.write_text(json.dumps(changed), encoding="utf-8")
                before = self.state.read_bytes(), self.output.read_bytes()
                refused = self.cli("sync")
                self.assertNotEqual(refused.returncode, 0)
                self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)

    def test_failed_output_replacement_preserves_files_and_allows_explicit_retry(self):
        self.interrupt_after_canonical_replace()
        before = self.state.read_bytes(), self.output.read_bytes()
        with patch.object(progress.os, "replace", side_effect=OSError("owned sync interruption")):
            with self.assertRaises(OSError):
                progress.sync_implementation_state(self.root)
        self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)
        self.assertEqual(list(self.output.parent.glob("*.tmp")), [])
        progress.sync_implementation_state(self.root)
        self.assertEqual(self.state.read_bytes(), before[0])
        self.assert_current_output()

    def test_custom_sync_destination_does_not_silently_repair_default_output(self):
        self.interrupt_after_canonical_replace()
        before = self.state.read_bytes(), self.output.read_bytes()
        custom = self.root / "outputs/custom-recovery.json"
        repaired = self.cli("sync", "--output", custom)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual((self.state.read_bytes(), self.output.read_bytes()), before)
        expected = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(custom.read_text(encoding="utf-8")),
            progress.extract_machine_readable_state(expected["milestone_journal"][-1],
                                                    expected),
        )
        self.assertTrue(progress.validate_journal(self.root))


if __name__ == "__main__":
    unittest.main()
