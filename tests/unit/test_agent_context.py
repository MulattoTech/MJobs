"""Synthetic Git worktrees test AI handoff without paid models or GitHub writes."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.agent_context import (bootstrap, checkpoint_path, load_checkpoint,
                                 relation, validate_checkpoint, write_checkpoint)


def git(root: Path, *args: str) -> str:
    result = subprocess.run([ "git", "-C", str(root), *args ],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


class AgentContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        (self.root / "planning").mkdir()
        (self.root / "planning/REQUIREMENT_INDEX.json").write_text(json.dumps({
            "summary": {"PARTIAL": 1},
            "requirements": [{"id": "MJ-REQ-004", "priority": "P0", "status": "PARTIAL"}],
        }), encoding="utf-8")
        (self.root / "planning/SESSION_HANDOFF.md").write_text(
            "Resume from existing tests.", encoding="utf-8",
        )
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")

    def checkpoint(self, task: str = "MJ-REQ-004", owner: str = "Gemini") -> Path:
        return write_checkpoint(self.root, task, "Complete bounded external operations", owner,
                                "Implement request cancellation negative tests",
                                ["pytest tests/unit/test_sources.py"],
                                ["live-provider verification unavailable"], [])

    def test_bootstrap_is_read_only_and_finds_task(self):
        value = bootstrap(self.root, "MJ-REQ-004")
        self.assertFalse(value["dirty"])
        self.assertEqual(value["requirement"]["status"], "PARTIAL")
        self.assertEqual(value["checkpoint_relation"], "NOT_FOUND")
        self.assertEqual(value["requirement_summary"]["PARTIAL"], 1)

    def test_checkpoint_roundtrip_and_resume(self):
        path = self.checkpoint()
        self.assertEqual(path, checkpoint_path(self.root, "MJ-REQ-004"))
        self.assertEqual(load_checkpoint(self.root, "MJ-REQ-004")["owner"], "Gemini")
        value = bootstrap(self.root, "MJ-REQ-004")
        self.assertEqual(value["checkpoint_relation"], "SAME_HEAD")
        self.assertIn("request cancellation", value["next_action"])

    def test_commit_advances_and_preserves_checkpoint(self):
        self.checkpoint()
        (self.root / "new.txt").write_text("work", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "progress")
        self.assertEqual(relation(self.root, load_checkpoint(self.root, "MJ-REQ-004")),
                         "ANCESTOR_REVERIFY_CHANGED_FILES")

    def test_branch_change_requires_ownership_reconciliation(self):
        self.checkpoint()
        git(self.root, "checkout", "-qb", "other")
        self.assertEqual(relation(self.root, load_checkpoint(self.root, "MJ-REQ-004")),
                         "OTHER_BRANCH_CHECK_OWNERSHIP")

    def test_reject_task_path_traversal(self):
        for task in ("../secret", "abc/def", "C:secret", "a\\b", ""):
            with self.subTest(task=task), self.assertRaises(ValueError):
                checkpoint_path(self.root, task)

    def test_dont_overwrite_another_owner(self):
        self.checkpoint()
        with self.assertRaises(ValueError):
            self.checkpoint(owner="Opus")

    def test_reject_false_test_result(self):
        value = {
            "schema_version": 1, "task_id": "MJ-REQ-004", "objective": "X",
            "owner": "Y", "branch": "main", "head_sha": "a" * 40,
            "next_action": "Z", "created_at": "2026-01-01T00:00:00Z",
            "requirements": [], "blockers": [],
            "tests": [{"command": "pytest", "result": "HOPEFULLY"}],
        }
        self.assertIn("tests[0] must have a command and PASS/FAIL/NOT_RUN",
                      validate_checkpoint(value))

    def test_missing_checkpoint_not_auto_generated(self):
        result = bootstrap(self.root, "MJ-REQ-009")
        self.assertIsNone(result["checkpoint"])
        self.assertNotIn("PASS", result["next_action"])
        self.assertFalse((self.root / "planning/agent_handoffs").exists())


if __name__ == "__main__":
    unittest.main()
