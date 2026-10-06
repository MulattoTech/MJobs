from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.agent_dashboard.supervisor import AgentSupervisor, classify_exit


class AgentDashboardSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        (self.root / "prompts").mkdir()
        for name in ("PARALLEL_CODEX_LANE.md", "PARALLEL_ANTIGRAVITY_LANE.md", "PARALLEL_OLLAMA_LANE.md"):
            (self.root / "prompts" / name).write_text("BASE PROMPT\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_exit_classification_distinguishes_gate_quota_and_success(self):
        self.assertEqual(classify_exit(0, "Do you approve the recommended design?"), "WAITING_APPROVAL")
        self.assertEqual(classify_exit(1, "RESOURCE_EXHAUSTED quota"), "BLOCKED_QUOTA")
        self.assertEqual(classify_exit(0, "work complete"), "COMPLETED")
        self.assertEqual(
            classify_exit(0, "do not stop for routine design/spec/plan approval"),
            "COMPLETED",
        )
        self.assertEqual(classify_exit(7, "unexpected failure"), "ERROR")

    def test_default_antigravity_model_is_flash_medium(self):
        supervisor = AgentSupervisor(self.root)
        self.assertEqual(supervisor.lane_config("antigravity")["model"], "gemini-3.8-flash-medium")
        command = supervisor.command_preview("antigravity")
        self.assertIn("gemini-3.8-flash-medium", command)

    def test_codex_prompt_contains_unattended_existing_issue_authority(self):
        supervisor = AgentSupervisor(self.root)
        prompt = supervisor._prompt("codex")
        self.assertIn("UNATTENDED EXISTING-ISSUE AUTHORITY", prompt)
        self.assertIn("do not stop for routine design/spec/plan approval", prompt)

    def test_dashboard_config_persists_in_git_common_coordination_dir(self):
        supervisor = AgentSupervisor(self.root)
        supervisor.update_config("ollama", {"model": "ollama/gemma4:26b", "auto_continue": False})
        saved = json.loads(supervisor.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["lanes"]["ollama"]["model"], "ollama/gemma4:26b")
        self.assertFalse(saved["lanes"]["ollama"]["auto_continue"])
        self.assertIn("mjobs-coordination", str(supervisor.config_path))


if __name__ == "__main__":
    unittest.main()
