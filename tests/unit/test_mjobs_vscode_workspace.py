"""Portable VS Code multi-root MJobs discovery, using only disposable local Git.

No network calls, MJobs main mutations, GitHub actions or AI processes.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.mjobs_vscode_workspace import (
    create_workspace, git_common_dir, independent_clones,
    is_mjobs_remote, linked_worktrees,
)


def git(path: Path, *args: str) -> str:
    call = subprocess.run(
        ["git", "-C", str(path), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False,
    )
    if call.returncode:
        raise AssertionError("Synthetic Git failed: " + call.stderr)
    return call.stdout.strip()


class MJobsVSCodeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-vscode-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / "MJobs"
        self.root.mkdir()
        git(self.root, "init")
        git(self.root, "checkout", "-b", "main")
        git(self.root, "config", "user.name", "MJobs Test")
        git(self.root, "config", "user.email", "mjobs@example.invalid")
        (self.root / "README.md").write_text("MJobs fixture\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-m", "feat: initial main")
        git(self.root, "remote", "add", "origin",
            "https://github.com/MulattoTech/MJobs.git")
        self.pinned = []
        for label, name in (
            ("codex", "MJobs-Codex-App"),
            ("antigravity", "MJobs-Antigravity-Matching"),
        ):
            location = self.parent / name
            git(self.root, "worktree", "add", "-b", "worker/" + label, str(location))
            self.pinned.append(location)
        self.optional = []
        for label in ("One", "Two"):
            location = self.parent / ("MJobs-Worker-" + label)
            git(self.root, "worktree", "add", "-b", "worker/" + label.lower(), str(location))
            self.optional.append(location)
        self.independent = self.parent / "MJobs-MissionControl-Launch"
        clone = subprocess.run(
            ["git", "clone", "--no-local", str(self.root), str(self.independent)],
            capture_output=True, text=True,
        )
        self.assertEqual(clone.returncode, 0, clone.stderr)
        git(self.independent, "remote", "set-url", "origin",
            "https://github.com/MulattoTech/MJobs.git")
        self.other = self.parent / "MJobs-Foreign"
        self.other.mkdir()
        git(self.other, "init")
        git(self.other, "remote", "add", "origin",
            "https://github.com/OtherCo/NotMJobs.git")
        self.output = self.parent / "MJobs-Test.generated.code-workspace"

    def test_separate_worktrees_and_independent_clones_are_identified_truthfully(self):
        main_common = git_common_dir(self.root)
        self.assertEqual(main_common, git_common_dir(self.pinned[0]))
        self.assertNotEqual(main_common, git_common_dir(self.independent))
        self.assertTrue(is_mjobs_remote(self.pinned[0]))
        self.assertTrue(is_mjobs_remote(self.independent))
        self.assertFalse(is_mjobs_remote(self.other))
        clones = independent_clones(self.root)
        self.assertEqual(len(clones), 1)
        self.assertEqual(Path(clones[0]["path"]), self.independent.resolve())
        self.assertEqual(len(linked_worktrees(self.root)), 5)

    def test_all_clones_and_pinned_lanes_plus_recent_are_opened(self):
        before = git(self.root, "rev-parse", "HEAD")
        report = create_workspace(self.root, self.output, recent=1)
        self.assertEqual(report["registered_worktrees"], 5)
        self.assertEqual(report["independent_clones"], 1)
        self.assertEqual(report["opened_linked_worktrees"], 3)  # pinned 2 + recent 1
        self.assertEqual(report["folder_count"], 5)
        view = json.loads(self.output.read_text(encoding="utf-8"))
        paths = {Path(x["path"]).resolve() for x in view["folders"]}
        self.assertIn(self.root.resolve(), paths)
        self.assertIn(self.independent.resolve(), paths)
        self.assertIn(self.pinned[0].resolve(), paths)
        self.assertIn(self.pinned[1].resolve(), paths)
        self.assertEqual(len(paths), len(view["folders"]))
        self.assertEqual(view["settings"]["git.autofetch"], False)
        registry = json.loads(Path(report["registry_file"]).read_text(encoding="utf-8"))
        self.assertEqual(len(registry["all_worktrees"]), 5)
        self.assertEqual(registry["available_linked_worktrees"], 4)
        self.assertEqual(registry["independent_clone_count"], 1)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")

    def test_all_mode_explicitly_includes_every_linked_git_folder(self):
        report = create_workspace(
            self.root, self.output, recent=0, all_worktrees=True
        )
        self.assertEqual(report["folder_count"], 6)
        self.assertEqual(report["opened_linked_worktrees"], 4)
        folders = json.loads(self.output.read_text(encoding="utf-8"))["folders"]
        self.assertEqual(len(folders), len({
            f["path"].lower() for f in folders
        }))

    def test_output_requires_explicit_generated_filename(self):
        unsafe = self.parent / "PersonalWorkspace.code-workspace"
        unsafe.write_text("SAVED USER CONTENT", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "generated"):
            create_workspace(self.root, unsafe)
        self.assertEqual(unsafe.read_text(encoding="utf-8"), "SAVED USER CONTENT")

    def test_rejects_other_remote_as_root(self):
        with self.assertRaisesRegex(ValueError, "MulattoTech/MJobs"):
            create_workspace(self.other, self.output)

    def test_large_worktree_inventory_requires_explicit_override(self):
        fake = []
        for number in range(151):
            fake.append({
                "path": str(self.parent / f"worktree-{number}"),
                "head": "0" * 40,
                "branch": "refs/heads/fake/" + str(number),
                "exists": True, "prunable": False,
                "detached": False, "commit_unix": number,
            })
        with patch("tools.mjobs_vscode_workspace.linked_worktrees",
                   return_value=fake), patch(
                       "tools.mjobs_vscode_workspace.independent_clones",
                       return_value=[],
                   ):
            with self.assertRaisesRegex(ValueError, "overload VS Code"):
                create_workspace(self.root, self.output, all_worktrees=True)
            self.assertFalse(self.output.exists())
            okay = create_workspace(
                self.root, self.output, all_worktrees=True, allow_huge=True
            )
            self.assertEqual(okay["folder_count"], 152)

    def test_repeated_generation_refreshes_workspace_without_git_changes(self):
        a = create_workspace(self.root, self.output, recent=1)
        b = create_workspace(self.root, self.output, recent=2)
        self.assertEqual(a["workspace_file"], b["workspace_file"])
        self.assertEqual(b["opened_linked_worktrees"], 4)
        self.assertEqual(b["folder_count"], 6)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
