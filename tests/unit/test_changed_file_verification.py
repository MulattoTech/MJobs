from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.verification.changed_file_verification import (
    build_verification_plan,
    collect_changed_paths,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class ChangedFileVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        git(self.root, "branch", "-M", "main")
        git(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")

    def test_always_checks_are_present_and_python_agent_context_is_focused(self):
        plan = build_verification_plan(
            self.root,
            ["tools/agent_context.py", "tests/unit/test_agent_context.py"],
        )

        displays = [check["display"] for check in plan["checks"]]
        self.assertIn("python -m tools.verification.catalogue", displays)
        self.assertIn("python -m tools.verification.progress validate", displays)
        self.assertIn(
            "python -m pytest -q tests/unit/test_agent_context.py tests/unit/test_agent_context_child_ids.py",
            displays,
        )
        self.assertEqual(plan["profile"], "focused")
        self.assertFalse(plan["recommend_full_verification"])

    def test_frontend_source_uses_matching_test_and_shared_quality_checks(self):
        (self.root / "apps").mkdir()
        (self.root / "apps/web").mkdir()
        (self.root / "apps/web/src").mkdir()
        test = self.root / "apps/web/src/theme.test.tsx"
        test.write_text("test\n", encoding="utf-8")

        plan = build_verification_plan(
            self.root,
            ["apps/web/src/theme.tsx", "apps/web/src/theme.test.tsx"],
        )
        displays = [check["display"] for check in plan["checks"]]

        self.assertIn("npm run typecheck --prefix apps/web", displays)
        self.assertIn("npm test --prefix apps/web -- src/theme.test.tsx", displays)
        self.assertEqual(plan["profile"], "focused")

    def test_dependency_migration_and_shared_contract_changes_recommend_full(self):
        for path in (
            "requirements.lock",
            "alembic/versions/0001_add_jobs.py",
            "apps/api/main.py",
            ".github/workflows/ci.yml",
        ):
            with self.subTest(path=path):
                plan = build_verification_plan(self.root, [path])
                self.assertEqual(plan["profile"], "broad")
                self.assertTrue(plan["recommend_full_verification"])

    def test_unknown_and_unmapped_path_recommends_full_without_silent_skip(self):
        plan = build_verification_plan(self.root, ["vendor/new-component.zzz"])

        self.assertIn("vendor/new-component.zzz", plan["changed_paths"])
        self.assertEqual(plan["profile"], "broad")
        self.assertTrue(plan["recommend_full_verification"])
        self.assertTrue(any("unknown" in reason.lower() or "unmapped" in reason.lower()
                            for check in plan["checks"] for reason in [check["rationale"]]))

    def test_collect_changed_paths_includes_committed_worktree_and_untracked_paths(self):
        (self.root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        (self.root / "unknown.py").write_text("value = 1\n", encoding="utf-8")
        git(self.root, "add", "dirty.txt")

        paths = collect_changed_paths(self.root, "origin/main")

        self.assertIn("dirty.txt", paths)
        self.assertIn("unknown.py", paths)

    def test_cli_dry_run_is_read_only_json(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.verification.changed_file_verification",
                "--root",
                str(self.root),
                "--base",
                "origin/main",
                "--path",
                "tools/agent_context.py",
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(value["profile"], "focused")
        self.assertTrue(all(check["display"] for check in value["checks"]))

    def test_agent_context_verify_is_available_as_dry_run(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.agent_context",
                "verify",
                "--root",
                str(self.root),
                "--base",
                "origin/main",
                "--path",
                "tools/agent_context.py",
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertFalse(value["recommend_full_verification"])
