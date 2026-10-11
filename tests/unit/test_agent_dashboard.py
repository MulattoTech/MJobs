from __future__ import annotations

import json
import subprocess
from unittest.mock import patch
import tempfile
import time
import unittest
from pathlib import Path

from tools.agent_dashboard.supervisor import AgentSupervisor, classify_exit
from tools.agent_dashboard.observability import (
    _antigravity_transcript_summary,
    _codex_rollout_summary,
    offload_layers,
    normalize_codex_rate_limits,
)


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



    def test_codex_rollout_summary_separates_current_turn_and_session_totals(self):
        rollout = self.root / "rollout-2026-10-06T19-43-32-01a113d0-ca3b-7280-b4ab-8d00937b6f38.jsonl"
        events = [
            {
                "timestamp": "2026-10-07T00:43:32Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "C:\\Tools\\Dev\\MJobs\\.worktrees\\codex-test",
                    "model": "gpt-6-luna",
                    "effort": "high",
                },
            },
            {
                "timestamp": "2026-10-07T00:43:40Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec"},
            },
            {
                "timestamp": "2026-10-07T00:43:45Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100000,
                            "cached_input_tokens": 80000,
                            "cache_write_input_tokens": 2000,
                            "output_tokens": 5000,
                            "reasoning_output_tokens": 1200,
                            "total_tokens": 105000,
                        },
                        "last_token_usage": {
                            "input_tokens": 30000,
                            "cached_input_tokens": 25000,
                            "cache_write_input_tokens": 500,
                            "output_tokens": 1000,
                            "reasoning_output_tokens": 300,
                            "total_tokens": 31000,
                        },
                        "model_context_window": 258400,
                    },
                },
            },
        ]
        rollout.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        result = _codex_rollout_summary(rollout)
        self.assertEqual(result["model"], "gpt-6-luna")
        self.assertEqual(result["current_turn"]["new_input_tokens"], 5000)
        self.assertEqual(result["current_turn"]["cache_read_tokens"], 25000)
        self.assertEqual(result["current_turn"]["cumulative_total_tokens"], 31000)
        self.assertAlmostEqual(
            result["current_turn"]["context_used_percent"],
            100 * 31000 / 258400,
            places=2,
        )
        self.assertEqual(result["session"]["new_input_tokens"], 20000)
        self.assertEqual(result["session"]["tool_calls"], 1)
        self.assertEqual(result["session"]["tool_breakdown"]["exec"], 1)

    def test_antigravity_transcript_summary_counts_tokens_thinking_and_tools(self):
        transcript = (
            self.root / "164f1767-595a-494c-8d6d-9a3fce8454c6"
            / ".system_generated" / "logs" / "transcript_full.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-10-07T00:05:41Z",
                "content": (
                    "EXECUTE NOW. issue #308 / MJ-REQ-017.S07; "
                    "worktree=C:/Tools/Dev/MJobs/.worktrees/antigravity-test. "
                    "Current preferred model: Gemini 3.8 Flash Medium"
                ),
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-10-07T00:05:42Z",
                "input_tokens": 1000,
                "cache_read_tokens": 4000,
                "output_tokens": 200,
                "thinking": "Inspect the repository carefully.",
                "tool_calls": [{"name": "run_command", "args": {}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-10-07T00:05:45Z",
                "content": "tool result",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-10-07T00:05:48Z",
                "input_tokens": 500,
                "cache_read_tokens": 4500,
                "output_tokens": 100,
                "tool_calls": [
                    {"name": "view_file", "args": {}},
                    {"name": "run_command", "args": {}},
                ],
            },
        ]
        transcript.write_text(
            "\n".join(json.dumps(step) for step in steps) + "\n",
            encoding="utf-8",
        )
        result = _antigravity_transcript_summary(transcript)
        self.assertEqual(
            result["conversation_id"],
            "164f1767-595a-494c-8d6d-9a3fce8454c6",
        )
        self.assertEqual(result["issue"], 308)
        self.assertEqual(result["requirement_id"], "MJ-REQ-017.S07")
        self.assertEqual(result["latest_context_tokens"], 5000)
        self.assertEqual(result["input_tokens"], 1500)
        self.assertEqual(result["cache_read_tokens"], 8500)
        self.assertEqual(result["output_tokens"], 300)
        self.assertEqual(result["cumulative_total_tokens"], 10300)
        self.assertEqual(result["planner_turns"], 2)
        self.assertEqual(result["thinking_steps"], 1)
        self.assertEqual(result["tool_calls"], 3)
        self.assertEqual(result["tool_breakdown"]["run_command"], 2)
        self.assertEqual(result["tool_breakdown"]["view_file"], 1)
        self.assertIn("Gemini 3.8 Flash Medium", result["model_hint"])

    def test_idle_ready_lane_without_claim_displays_idle_unassigned(self):
        lane_dir = self.root / "planning" / "lanes" / "antigravity"
        lane_dir.mkdir(parents=True)
        (lane_dir / "CONTINUATION.json").write_text(
            json.dumps({"status": "IDLE_READY"}),
            encoding="utf-8",
        )
        supervisor = AgentSupervisor(self.root)
        supervisor.telemetry_snapshot = lambda: {
            "processes": [],
            "gpu": {"available": False, "by_pid": {}},
            "codex_usage": {"available": False},
            "ollama": {"available": True, "models": []},
        }
        supervisor.coordination = lambda: {"claims": {}, "lock": None}
        supervisor.git_state = lambda: {
            "branch": "main",
            "head": "abc",
            "origin_main": "abc",
            "dirty": [],
            "worktrees": [],
        }
        state = supervisor.snapshot()
        self.assertEqual(
            state["lanes"]["antigravity"]["runtime"]["status"],
            "IDLE_UNASSIGNED",
        )
        self.assertIn(
            "no active issue claim",
            state["lanes"]["antigravity"]["runtime"]["last_reason"],
        )

    def test_gpu_offload_percent_maps_to_model_blocks(self):
        self.assertEqual(offload_layers(30, 100), 30)
        self.assertEqual(offload_layers(30, 50), 15)
        self.assertEqual(offload_layers(30, 25), 8)
        self.assertEqual(offload_layers(30, 0), 0)

    def test_codex_rate_limit_normalization_reports_remaining_percent(self):
        result = normalize_codex_rate_limits({
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "planType": "prolite",
                "primary": {"usedPercent": 8, "windowDurationMins": 10080, "resetsAt": 1791779899},
                "secondary": None,
                "credits": {"hasCredits": True, "unlimited": False, "balance": "1902.97"},
                "spendControlReached": False,
            },
            "rateLimitResetCredits": {"availableCount": 1},
        })
        self.assertTrue(result["available"])
        self.assertEqual(result["used_percent"], 8)
        self.assertEqual(result["remaining_percent"], 92)
        self.assertEqual(result["window_minutes"], 10080)
        self.assertEqual(result["reset_credits_available"], 1)


    def test_antigravity_quota_error_is_detected_from_native_wording(self):
        message = (
            "RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
            "Please upgrade your subscription to increase your limits. Resets in 4h37m15s."
        )
        self.assertIn(
            "Individual quota reached",
            AgentSupervisor._quota_error_from_text(message) or "",
        )

    def test_terminal_sources_begin_at_current_run_offset(self):
        supervisor = AgentSupervisor(self.root)
        log = supervisor.log_path("codex")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("old output\nnew output\n", encoding="utf-8")
        supervisor.runtime["lanes"]["codex"].update({
            "run_id": "run-1",
            "log_start_offset": len("old output\n".encode()),
            "native_log_path": None,
        })
        info = supervisor.terminal_sources("codex")
        self.assertEqual(info["run_id"], "run-1")
        self.assertEqual(info["sources"][0]["start_offset"], len("old output\n".encode()))

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


    def test_stale_telemetry_returns_cached_value_then_refreshes_in_background(self):
        supervisor = AgentSupervisor(self.root)
        supervisor._telemetry_cache["slow"] = (
            time.monotonic() - 30,
            {"value": "old"},
        )

        def loader():
            time.sleep(0.1)
            return {"value": "new"}

        value = supervisor._cached_telemetry("slow", 1.0, loader)
        self.assertEqual(value, {"value": "old"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if supervisor._telemetry_cache["slow"][1] == {"value": "new"}:
                break
            time.sleep(0.02)
        self.assertEqual(supervisor._telemetry_cache["slow"][1], {"value": "new"})

    def test_codex_prompt_contains_unattended_existing_issue_authority(self):
        supervisor = AgentSupervisor(self.root)
        prompt = supervisor._prompt("codex")
        self.assertTrue(prompt.startswith("EXECUTE NOW."))
        self.assertIn("UNATTENDED EXISTING-ISSUE AUTHORITY", prompt)
        self.assertIn("do not stop for routine design/spec/plan approval", prompt)
        command = supervisor._command("codex")
        self.assertIn("danger-full-access", command)
        self.assertNotIn("--approve-for-me", command)

    def test_codex_runner_resolves_an_existing_path_deterministically(self):
        executable = self.root / "codex"
        executable.write_text("synthetic executable placeholder", encoding="utf-8")
        with patch("tools.agent_dashboard.supervisor.shutil.which", return_value=str(executable)):
            prefix = AgentSupervisor(self.root)._runner_prefix("codex")
        self.assertEqual(prefix, [str(executable)])

    def test_installed_codex_runner_resolves_to_launchable_prefix(self):
        supervisor = AgentSupervisor(self.root)
        try:
            prefix = supervisor._runner_prefix("codex")
        except FileNotFoundError:
            self.skipTest("Optional host-installed Codex CLI is absent on this runner")
        self.assertTrue(prefix)
        self.assertTrue(any("codex" in part.casefold() for part in prefix))


    def test_prompt_frontloads_live_lane_claim(self):
        supervisor = AgentSupervisor(self.root)
        supervisor.bus.mkdir(parents=True, exist_ok=True)
        (supervisor.bus / "claims.json").write_text(
            json.dumps({
                "schema_version": 1,
                "claims": {
                    "ollama": {
                        "issue": 318,
                        "requirement_id": "MJ-REQ-063.D01",
                        "branch": "ollama/greenhouse",
                        "worktree": "C:/worktrees/greenhouse",
                    }
                },
            }),
            encoding="utf-8",
        )
        prompt = supervisor._prompt("ollama")
        self.assertIn("ACTIVE CLAIM: issue #318 / MJ-REQ-063.D01", prompt[:500])
        self.assertIn("Do NOT switch to the repository's global active issue", prompt[:700])


    def test_codex_command_uses_claimed_worktree(self):
        supervisor = AgentSupervisor(self.root)
        supervisor.bus.mkdir(parents=True, exist_ok=True)
        claimed = str(self.root / ".worktrees" / "codex-issue")
        (supervisor.bus / "claims.json").write_text(
            json.dumps({
                "schema_version": 1,
                "claims": {
                    "codex": {
                        "issue": 111,
                        "requirement_id": "MJ-REQ-110",
                        "branch": "codex/issue",
                        "worktree": claimed,
                    }
                },
            }),
            encoding="utf-8",
        )
        command = supervisor.command_preview("codex")
        self.assertIn(claimed, command)

    def test_writable_lane_requires_complete_claim(self):
        supervisor = AgentSupervisor(self.root)
        with self.assertRaises(RuntimeError):
            supervisor._workspace_for_lane("ollama")

    def test_dashboard_config_persists_in_git_common_coordination_dir(self):
        supervisor = AgentSupervisor(self.root)
        supervisor.update_config("ollama", {"model": "ollama/gemma4:26b", "auto_continue": False})
        saved = json.loads(supervisor.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["lanes"]["ollama"]["model"], "ollama/gemma4:26b")
        self.assertFalse(saved["lanes"]["ollama"]["auto_continue"])
        self.assertIn("mjobs-coordination", str(supervisor.config_path))


if __name__ == "__main__":
    unittest.main()
