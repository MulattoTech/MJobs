import unittest
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tools.coordination.lane_coordination import claim_record, claim_key, put_claim, release_claim_key, paths_overlap


class LaneCoordinationTests(unittest.TestCase):
    def test_exact_and_recursive_claim_overlap(self):
        self.assertTrue(paths_overlap("services/ai/**", "services/ai/tasks.py"))
        self.assertTrue(paths_overlap("apps/web/src/main.tsx", "apps/web/src/main.tsx"))
        self.assertFalse(paths_overlap("services/ai/**", "services/discovery/sources/remotive.py"))

    def test_recursive_claim_does_not_match_similar_prefix(self):
        self.assertFalse(paths_overlap("services/ai/**", "services/audit/report.py"))

    def test_codex_child_claims_coexist_and_release_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = init_git_root(Path(directory))
            codex_a = claim_record("codex", 96, "MJ-REQ-095", "branch-a", "tree-a", ["services/documents/tailor.py"])
            codex_b = claim_record("codex", 117, "MJ-REQ-116", "branch-b", "tree-b", ["apps/web/src/matching-label.tsx"])

            self.assertEqual(put_claim(root, claim_key("codex", "a"), codex_a), ("CLAIMED", []))
            self.assertEqual(put_claim(root, claim_key("codex", "b"), codex_b), ("CLAIMED", []))
            released = release_claim_key(root, claim_key("codex", "a"))

            self.assertEqual(released["issue"], 96)
            claims = json.loads((root / ".git" / "mjobs-coordination" / "claims.json").read_text())
            self.assertEqual(set(claims["claims"]), {"codex:b"})

    def test_same_lane_path_overlap_is_rejected_without_replacing_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = init_git_root(Path(directory))
            first = claim_record("codex", 96, "MJ-REQ-095", "branch-a", "tree-a", ["services/documents/**"])
            second = claim_record("codex", 117, "MJ-REQ-116", "branch-b", "tree-b", ["services/documents/tailor.py"])
            put_claim(root, "codex:a", first)

            result, conflicts = put_claim(root, "codex:b", second)

            self.assertEqual(result, "CONFLICT")
            self.assertEqual(conflicts[0]["claim_key"], "codex:a")
            claims = json.loads((root / ".git" / "mjobs-coordination" / "claims.json").read_text())
            self.assertEqual(set(claims["claims"]), {"codex:a"})

    def test_concurrent_claim_updates_do_not_lose_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = init_git_root(Path(directory))
            script = Path(__file__).resolve().parents[2] / "tools" / "coordination" / "lane_coordination.py"
            def add_claim(index):
                return subprocess.run([
                    sys.executable, str(script), "claim", "--lane", "codex", "--child-id", str(index),
                    "--issue", str(index), "--requirement", f"MJ-REQ-{index}",
                    "--branch", f"branch-{index}", "--worktree", f"tree-{index}", "--path", f"path/{index}.py",
                ], cwd=root, capture_output=True, text=True)

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(add_claim, range(8)))

            self.assertEqual([result.returncode for result in results], [0] * 8)
            self.assertTrue(all(json.loads(result.stdout)["result"] == "CLAIMED" for result in results))
            claims = json.loads((root / ".git" / "mjobs-coordination" / "claims.json").read_text())
            self.assertEqual(len(claims["claims"]), 8)

    def test_duplicate_child_id_cannot_replace_active_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = init_git_root(Path(directory))
            original = claim_record("codex", 96, "MJ-REQ-095", "branch-a", "tree-a", ["services/documents/tailor.py"])
            replacement = claim_record("codex", 117, "MJ-REQ-116", "branch-b", "tree-b", ["apps/web/src/main.tsx"])
            put_claim(root, "codex:child", original)

            result, conflicts = put_claim(root, "codex:child", replacement)

            self.assertEqual(result, "CLAIM_EXISTS")
            self.assertEqual(conflicts[0]["issue"], 96)
            claims = json.loads((root / ".git" / "mjobs-coordination" / "claims.json").read_text())
            self.assertEqual(claims["claims"]["codex:child"]["issue"], 96)

    def test_child_claims_preserve_legacy_lane_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = init_git_root(Path(directory))
            legacy = claim_record("codex", 116, "MJ-REQ-115", "legacy-branch", "legacy-tree", ["services/discovery/manual_capture.py"])
            child = claim_record("codex", 96, "MJ-REQ-095", "child-branch", "child-tree", ["services/documents/tailor.py"])
            self.assertEqual(put_claim(root, "codex", legacy), ("CLAIMED", []))
            self.assertEqual(put_claim(root, "codex:resume", child), ("CLAIMED", []))

            release_claim_key(root, "codex:resume")

            claims = json.loads((root / ".git" / "mjobs-coordination" / "claims.json").read_text())
            self.assertEqual(set(claims["claims"]), {"codex"})
            self.assertEqual(claims["claims"]["codex"]["issue"], 116)


def init_git_root(root: Path) -> Path:
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    return root


if __name__ == "__main__":
    unittest.main()
