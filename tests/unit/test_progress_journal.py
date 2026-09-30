"""Checkpoint writes must preserve the last resumable implementation state."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.verification.progress import append_milestone, validate_journal


class ProgressJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "planning").mkdir()
        (self.root / "docs/evidence").mkdir(parents=True)
        (self.root / "docs/evidence/checkpoint.json").write_text("{}", encoding="utf-8")
        self.state_path = self.root / "planning/IMPLEMENTATION_STATE.json"
        self.state_path.write_text(json.dumps({"schema_version": 1, "current_phase": "BEFORE"}),
                                   encoding="utf-8")
        self.entry = {
            "milestone_id": "M3-TEST",
            "timestamp": "2026-09-30T20:00:00Z",
            "current_phase": "M3_VERIFIED",
            "completed_steps": ["Mock journey verified"],
            "files_added": ["docs/evidence/checkpoint.json"],
            "files_modified": ["planning/IMPLEMENTATION_STATE.json"],
            "tests_completed": ["python -m unittest tests.unit.test_progress_journal"],
            "tests_remaining": ["Live external submission"],
            "migrations_applied": [],
            "live_integrations_tested": [],
            "known_errors": [],
            "known_limitations": ["Real external submission remains disabled"],
            "last_safe_checkpoint": {
                "git_commit": "a" * 40,
                "evidence_path": "docs/evidence/checkpoint.json",
            },
        }

    def test_append_is_durable_and_resumable(self):
        append_milestone(self.state_path, self.entry)
        restored = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["current_phase"], "M3_VERIFIED")
        self.assertEqual(restored["milestone_journal"], [self.entry])
        self.assertEqual(validate_journal(self.root), [])

    def test_duplicate_and_missing_evidence_leave_prior_state_intact(self):
        append_milestone(self.state_path, self.entry)
        before = self.state_path.read_bytes()
        with self.assertRaises(ValueError):
            append_milestone(self.state_path, self.entry)
        self.assertEqual(self.state_path.read_bytes(), before)
        changed = {**self.entry, "milestone_id": "M3-NEXT",
                   "last_safe_checkpoint": {"git_commit": "b" * 40,
                                            "evidence_path": "docs/evidence/missing.json"}}
        with self.assertRaises(ValueError):
            append_milestone(self.state_path, changed)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_failed_atomic_replace_preserves_last_checkpoint(self):
        before = self.state_path.read_bytes()
        with patch("tools.verification.progress.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                append_milestone(self.state_path, self.entry)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(list((self.root / "planning").glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
