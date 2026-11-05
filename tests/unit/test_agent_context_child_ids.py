"""Canonical child requirements remain addressable across handoff processes."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.agent_context import (
    checkpoint_path, load_checkpoint, validate_checkpoint, write_checkpoint,
)


REPO = Path(__file__).resolve().parents[2]
CHILD = "MJ-REQ-017.S07"


class AgentContextChildIDTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git("init", "-q", "-b", "child-id-test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.root / "planning").mkdir()
        (self.root / "planning/REQUIREMENT_INDEX.json").write_text(json.dumps({
            "summary": {"PARTIAL": 1},
            "requirements": [{"id": CHILD, "priority": "P5", "status": "PARTIAL"}],
        }), encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "synthetic base")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "tools.agent_context", *args, "--root", str(self.root)],
            cwd=REPO, capture_output=True, text=True, check=False, timeout=15,
        )

    def checkpoint(self, task=CHILD, owner="test-owner"):
        return write_checkpoint(
            self.root, task, "Preserve child-task progress", owner,
            "Verify child-task recovery", [], ["protected CI"], [],
        )

    def test_every_canonical_requirement_has_a_distinct_safe_checkpoint_path(self):
        index = json.loads((REPO / "planning/REQUIREMENT_INDEX.json").read_text(
            encoding="utf-8"))
        ids = [item["id"] for item in index["requirements"]]
        self.assertIn(CHILD, ids)
        paths = set()
        for task in ids:
            with self.subTest(task=task):
                path = checkpoint_path(self.root, task)
                self.assertEqual(path.parent, self.root / "planning/agent_handoffs")
                self.assertEqual(path.name, f"{task}.json")
                self.assertNotIn(path, paths)
                paths.add(path)
        self.assertFalse((self.root / "planning/agent_handoffs").exists())

    def test_child_bootstrap_without_checkpoint_is_missing_not_invalid(self):
        before = self.git("status", "--porcelain")
        result = self.cli("bootstrap", "--task", CHILD)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["requirement"]["id"], CHILD)
        self.assertEqual(value["checkpoint_relation"], "NOT_FOUND")
        self.assertEqual(value["routing_state"], "EXECUTE")
        self.assertNotIn("checkpoint_error", value)
        self.assertEqual(self.git("status", "--porcelain"), before)

    def test_child_checkpoint_fresh_process_resume_preserves_parent_and_sibling(self):
        tasks = ("MJ-REQ-017", CHILD, "MJ-REQ-017.S08")
        for task in tasks:
            created = self.cli(
                "checkpoint", "--task", task, "--objective", f"Progress for {task}",
                "--owner", "test-owner", "--next-action", f"Continue {task}",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
        saved = {task: checkpoint_path(self.root, task).read_bytes() for task in tasks}
        for command in ("validate", "resume"):
            result = self.cli(command, "--task", CHILD)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            value = json.loads(result.stdout)
            self.assertEqual(value["checkpoint"]["task_id"], CHILD)
            self.assertEqual(value["checkpoint"]["requirements"], [CHILD])
            self.assertEqual(value["checkpoint_relation"], "SAME_HEAD")
            self.assertEqual(value["next_action"], f"Continue {CHILD}")
        for task in tasks:
            self.assertEqual(checkpoint_path(self.root, task).read_bytes(), saved[task])

    def test_malformed_dotted_ids_and_traversal_still_refuse_writes(self):
        original = self.checkpoint("MJ-REQ-017")
        saved = original.read_bytes()
        valid = load_checkpoint(self.root, "MJ-REQ-017")
        invalid = (
            "../MJ-REQ-017.S07", "..\\MJ-REQ-017.S07", "MJ-REQ-017.S07/other",
            "MJ-REQ-017.S07\\other", "MJ-REQ-017.S07:stream", "MJ-REQ-017..S07",
            "MJ-REQ-017.S07.json", "MJ-REQ-017.S07.", "MJ-REQ-017.S07 ",
            "MJ-REQ-017.S07\n", "MJ-REQ-17.S07", "MJ-REQ-017.S7",
            "MJ-REQ-017.S007", "MJ-REQ-017.s07", "MJ-REQ-017.Ｓ07",
            "MJ-REQ-017.S٠٧", "task.name", "A" * 72, "", ".", "..",
        )
        for task in invalid:
            with self.subTest(task=task):
                with self.assertRaises(ValueError):
                    checkpoint_path(self.root, task)
                self.assertIn("task_id has invalid characters", validate_checkpoint({
                    **valid, "task_id": task,
                }))
                with self.assertRaises(ValueError):
                    self.checkpoint(task)
        self.assertEqual(original.read_bytes(), saved)
        self.assertEqual(list(original.parent.iterdir()), [original])
        for task in ("A", "A" * 71, "owned_task-123"):
            self.assertEqual(checkpoint_path(self.root, task).name, f"{task}.json")

    def test_child_checkpoint_keeps_owner_and_exact_identity_checks(self):
        path = self.checkpoint()
        saved = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "different task branch/owner"):
            self.checkpoint(owner="another-owner")
        self.assertEqual(path.read_bytes(), saved)
        value = json.loads(saved)
        value["task_id"] = "MJ-REQ-017.S08"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "task_id mismatch"):
            load_checkpoint(self.root, CHILD)

    def test_child_checkpoint_cannot_escape_through_a_linked_directory(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        handoffs = self.root / "planning/agent_handoffs"
        try:
            handoffs.symlink_to(outside.name, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Directory symlinks unavailable: {type(exc).__name__}")
        self.addCleanup(handoffs.unlink)
        with self.assertRaisesRegex(ValueError, "checkpoint path escapes repository"):
            self.checkpoint()
        self.assertEqual(list(Path(outside.name).iterdir()), [])
