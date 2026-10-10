"""Mission Control profile launch, safety boundaries, and canonical status tests."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.agent_dashboard.server import create_app
from tools.agent_dashboard.mission_control import MissionControl, LaunchRequest
from tools.coordination.lane_coordination import claim_key


def run(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, check=True, text=True)
    return result.stdout.strip()


class MissionControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        run(self.root, "checkout", "-b", "main")
        run(self.root, "-c", "user.name=Test",
            "-c", "user.email=test@example.test", "commit", "--allow-empty", "-m", "init")
        run(self.root, "update-ref", "refs/remotes/origin/main", "HEAD")
        prompts = self.root / "prompts" / "dual-agent"
        prompts.mkdir(parents=True)
        for slug in ("codex-luna-high", "antigravity-opus", "claude-release-audit"):
            (prompts / (slug + ".md")).write_text("GOAL and verified implementation rules\n", encoding="utf-8")
        planning = self.root / "planning"
        planning.mkdir()
        (planning / "CAPABILITY_MATRIX.json").write_text(json.dumps({
            "timestamp": "2026-10-09T21:58:04Z",
            "requirements": [
                {"requirement_id": "MJ-REQ-001", "result": "PASS"},
                {"requirement_id": "MJ-REQ-002", "result": "PARTIAL"},
                {"requirement_id": "MJ-REQ-003", "result": "NOT_RUN"},
            ],
        }), encoding="utf-8")
        (planning / "REFERENCE_GAP_MATRIX.json").write_text(json.dumps({
            "original_catalogue_count": 3, "reference_gap_count": 2,
            "canonical_ids": ["MJ-REF-001", "MJ-REF-002"],
            "generated_at": "2026-10-09T20:00:00Z",
            "result_by_id": {"MJ-REF-001": "PASS", "MJ-REF-002": "NOT_RUN"},
        }), encoding="utf-8")
        (planning / "IMPLEMENTATION_STATE.json").write_text(json.dumps({
            "release_readiness": {"result": "UNKNOWN"},
            "current_milestone": "SAFE_TEST",
        }), encoding="utf-8")
        (planning / "ISSUE_SEED.jsonl").write_text("\n".join(json.dumps({
            "id": "MJ-REQ-%03d" % i,
            "domain": ("application_automation" if i <= 2 else "matching_ai"),
            "priority": "P0",
        }) for i in range(1, 4)) + "\n", encoding="utf-8")
        self.app = create_app(self.root)
        self.client = TestClient(self.app)
        self.manager: MissionControl = self.app.state.mission_control

    def tearDown(self):
        self.temporary.cleanup()

    def test_progress_is_derived_from_canonical_matrix_without_mutations(self):
        response = self.client.get("/api/progress")
        self.assertEqual(response.status_code, 200)
        progress = response.json()
        self.assertTrue(progress["available"], progress)
        self.assertEqual(progress["original"]["pass"], 1)
        self.assertEqual(progress["original"]["partial"], 1)
        self.assertEqual(progress["original"]["not_run"], 1)
        self.assertEqual(progress["original"]["accepted_percent"], 33.3)
        self.assertEqual(progress["reference"]["pass"], 1)
        self.assertEqual(progress["reference"]["accepted_percent"], 50.0)
        self.assertEqual(progress["release_readiness"], "UNKNOWN")
        self.assertFalse(progress["checkpoint_current"])
        self.assertFalse((self.root / "docs" / "PROGRESS_CARD.json").exists())

    def test_dirty_root_main_does_not_block_isolated_creation(self):
        (self.root / "unsaved-main.txt").write_text("Do not lose this", encoding="utf-8")
        data = self.manager.prepare("codex")
        self.assertEqual(data["branch"], "codex/priority-app-automation-20261009")
        self.assertFalse(data["dirty"])
        self.assertTrue((self.root / "unsaved-main.txt").exists())
        self.assertEqual(run(self.root, "branch", "--show-current"), "main")
        self.assertIn("?? unsaved-main.txt", run(self.root, "status", "--porcelain"))
        self.assertEqual(run(Path(data["workspace"]), "branch", "--show-current"), data["branch"])

    def test_preserves_dirty_owned_worktree_on_repeat_prepare(self):
        first = self.manager.prepare("codex")
        worktree = Path(first["workspace"])
        user_file = worktree / "my-notes.txt"
        user_file.write_text("unfinished work", encoding="utf-8")
        second = self.manager.prepare("codex")
        self.assertTrue(second["dirty"])
        self.assertEqual(user_file.read_text(), "unfinished work")
        self.assertTrue(any("preserved" in x for x in second["warnings"]))

    def test_does_not_take_over_unrelated_directory(self):
        dest = self.root.parent / "MJobs-Antigravity-Matching"
        dest.mkdir()
        marker = dest / "important.txt"
        marker.write_text("Do not delete", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.manager.prepare("antigravity")
        self.assertEqual(marker.read_text(), "Do not delete")

    def test_profile_and_cross_origin_launch_are_rejected(self):
        token = self.client.get("/api/launcher/options").json()["token"]
        request = {
            "profile": "codex-luna-high", "model": "gpt-6-luna",
            "effort": "high", "agent": "", "instructions": "",
        }
        self.assertEqual(self.client.post("/api/launcher/codex/launch", json=request).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/launcher/codex/launch",
            headers={"X-MJobs-Launch-Token": token, "Origin": "https://evil.example"},
            json=request,
        ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/launcher/antigravity/launch",
            headers={"X-MJobs-Launch-Token": token},
            json=request,
        ).status_code, 409)
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())

    def test_start_runs_only_on_explicit_authorized_action(self):
        token = self.client.get("/api/launcher/options").json()["token"]
        with patch.object(self.app.state.supervisor, "_runner_prefix", return_value=["stub"]):
            with patch.object(self.app.state.supervisor, "start",
                              return_value={"status": "RUNNING", "pid": 12345}) as started:
                reply = self.client.post(
                    "/api/launcher/codex/launch",
                    headers={"X-MJobs-Launch-Token": token},
                    json={"profile": "codex-luna-high", "model": "gpt-6-luna",
                          "effort": "high", "agent": "", "instructions": "Do not submit.",
                          "auto_continue": False},
                )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["pid"], 12345)
        started.assert_called_once()
        cfg = self.app.state.supervisor.lane_config("codex")
        self.assertEqual(cfg["launch_profile"], "codex-luna-high")
        self.assertEqual(cfg["prompt_override"], "Do not submit.")
        self.assertFalse(cfg["auto_continue"])

    def test_cli_agents_have_distinct_path_claim_keys(self):
        self.assertEqual(claim_key("codex", "launch"), "codex:launch")
        self.assertEqual(claim_key("antigravity", "launch"), "antigravity:launch")
        self.assertEqual(claim_key("claude", "launch"), "claude:launch")

    def test_runner_cli_commands_are_validated_and_preview_redacts_prompt(self):
        sup = self.app.state.supervisor
        for lane, slug, model in (("codex", "codex-luna-high", "gpt-6-luna"),
                                  ("antigravity", "antigravity-opus", "opus"),
                                  ("claude", "claude-release-audit", "sonnet")):
            cfg = {"prompt_file": "prompts/dual-agent/" + slug + ".md",
                   "launch_workspace": str(self.root), "launch_profile": slug, "model": model}
            sup.update_config(lane, cfg)
            command = sup.command_preview(lane)
            self.assertIn("<prompt>", command)
            self.assertNotIn("GOAL and verified implementation rules\n", " ".join(command))
        self.assertIn("acceptEdits", sup.command_preview("claude"))
        self.assertNotIn("--dangerously-skip-permissions", sup.command_preview("antigravity"))

    def test_mission_instance_settings_do_not_override_older_dashboard(self):
        from tools.agent_dashboard.supervisor import AgentSupervisor
        legacy = self.app.state.supervisor
        mission = AgentSupervisor(self.root, mission_instance=True)
        self.assertNotEqual(mission.config_path, legacy.config_path)
        self.assertNotEqual(mission.runtime_path, legacy.runtime_path)
        self.assertFalse(mission.lane_config("codex")["auto_continue"])
        mission.update_config("codex", {"model": "test-model", "auto_continue": False})
        self.assertNotEqual(legacy.lane_config("codex")["model"], "test-model")

    def test_live_acceptance_reflects_matrix_changes_without_writing_checkpoint(self):
        first = self.client.get("/api/progress").json()
        matrix_path = self.root / "planning" / "CAPABILITY_MATRIX.json"
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        matrix["requirements"][1]["result"] = "PASS"
        matrix["timestamp"] = "2026-10-09T23:00:00Z"
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        second = self.client.get("/api/progress").json()
        self.assertEqual(first["original"]["pass"], 1)
        self.assertEqual(second["original"]["pass"], 2)
        self.assertEqual(second["original_as_of"], "2026-10-09T23:00:00Z")
        self.assertFalse(second["checkpoint_current"])


if __name__ == "__main__":
    unittest.main()
