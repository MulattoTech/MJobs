"""Checkpoint writes must preserve the last resumable implementation state."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.verification.progress import append_milestone, resume_snapshot, validate_journal


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

    def test_journal_survives_writer_exit_and_accepts_a_future_process_milestone(self):
        writer = """
import json, sys
from pathlib import Path
from tools.verification.progress import append_milestone
append_milestone(Path(sys.argv[1]), json.loads(sys.argv[2]))
"""
        resume = """
import json, sys
from pathlib import Path
from tools.verification.progress import append_milestone, validate_journal
state_path = Path(sys.argv[1])
entry = json.loads(sys.argv[2])
state = json.loads(state_path.read_text(encoding='utf-8'))
assert state['current_phase'] == 'M3_VERIFIED'
assert state['milestone_journal'][-1]['milestone_id'] == 'M3-TEST'
assert not validate_journal(state_path.parent.parent)
entry['milestone_id'] = 'M4-RESUMED'
entry['timestamp'] = '2026-09-30T20:01:00Z'
entry['current_phase'] = 'M4_RESUMED'
entry['completed_steps'] = ['Resumed from the persisted checkpoint in a new process']
append_milestone(state_path, entry)
"""
        for script in (writer, resume):
            completed = subprocess.run(
                [sys.executable, "-c", script, str(self.state_path), json.dumps(self.entry)],
                cwd=Path(__file__).resolve().parents[2],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        resumed = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["current_phase"], "M4_RESUMED")
        self.assertEqual([item["milestone_id"] for item in resumed["milestone_journal"]],
                         ["M3-TEST", "M4-RESUMED"])
        self.assertEqual(validate_journal(self.root), [])

    def test_resume_snapshot_exposes_latest_checkpoint_and_refuses_corrupt_journal(self):
        append_milestone(self.state_path, self.entry)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["active_development_issue"] = {
            "issue": 9,
            "requirement_id": "MJ-REQ-008",
            "next_action": "Run the resumed-work acceptance check",
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        snapshot = resume_snapshot(self.root)
        self.assertEqual(snapshot["current_phase"], "M3_VERIFIED")
        self.assertEqual(snapshot["active_development_issue"]["issue"], 9)
        self.assertEqual(snapshot["latest_milestone"]["milestone_id"], "M3-TEST")
        self.assertEqual(snapshot["last_safe_checkpoint"], self.entry["last_safe_checkpoint"])

        (self.root / "docs/evidence/checkpoint.json").unlink()
        with self.assertRaisesRegex(ValueError, "Checkpoint evidence is missing"):
            resume_snapshot(self.root)

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

    def test_milestone_chronology_uses_actual_utc_time(self):
        append_milestone(self.state_path, self.entry)
        before = self.state_path.read_bytes()
        earlier = {**self.entry, "milestone_id": "M3-EARLIER",
                   "timestamp": "2026-09-30T19:59:59+00:00"}
        with self.assertRaises(ValueError):
            append_milestone(self.state_path, earlier)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_corrupt_prior_checkpoint_is_not_silently_extended(self):
        append_milestone(self.state_path, self.entry)
        (self.root / "docs/evidence/checkpoint.json").unlink()
        (self.root / "docs/evidence/next.json").write_text("{}", encoding="utf-8")
        before = self.state_path.read_bytes()
        next_entry = {**self.entry, "milestone_id": "M3-NEXT",
                      "timestamp": "2026-09-30T20:01:00Z",
                      "last_safe_checkpoint": {"git_commit": "b" * 40,
                                               "evidence_path": "docs/evidence/next.json"}}
        with self.assertRaisesRegex(ValueError, "Existing milestone journal is invalid"):
            append_milestone(self.state_path, next_entry)
        self.assertEqual(self.state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
