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
from tools.agent_dashboard.model_catalog import ModelCatalog, _choice
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
        prompts = self.root / "prompts" / "codex"
        prompts.mkdir(parents=True)
        for slug in ("codex-general", "codex-luna-high", "codex-sol-6-1-high", "codex-astra-high"):
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
        # Never start installed CLI discovery inside disposable test repos:
        # a Windows child process can keep the temporary directory in use.
        def fake_discovery(_catalog, lane):
            choices = {
                "codex": [
                    _choice("gpt-6-luna","Luna",["medium","high"],"high"),
                    _choice("gpt-6.1-sol","Sol",["medium","high","xhigh"],"high"),
                ],
                "antigravity": [_choice("opus","Opus",["high"],"high")],
                "claude": [_choice("sonnet","Sonnet",["high"],"high")],
            }
            return choices[lane], "fixture_cli_catalog"
        self.discovery_patch = patch.object(
            ModelCatalog,"_discover",autospec=True,side_effect=fake_discovery
        )
        self.discovery_patch.start()
        self.app = create_app(self.root)
        self.client = TestClient(self.app)
        self.manager: MissionControl = self.app.state.mission_control

    def tearDown(self):
        self.discovery_patch.stop()
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
        self.assertEqual(data["branch"], "main")
        self.assertTrue(data["dirty"])
        self.assertEqual(Path(data["workspace"]), self.root)
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
        dest = self.root.parent / "MJobs-Unrelated-Workspace"
        dest.mkdir()
        marker = dest / "important.txt"
        marker.write_text("Do not delete", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.prepare("unsupported")
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
        request = {"profile": "codex-luna-high", "model": "gpt-6-luna",
                   "effort": "high", "agent": "Application Architect",
                   "instructions": "Do not submit.", "auto_continue": False}
        headers = {"X-MJobs-Launch-Token": token}
        preview = self.client.post("/api/launcher/codex/preview",
                                   headers=headers, json=request)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())
        with patch.object(self.app.state.supervisor, "_runner_prefix", return_value=["stub"]):
            with patch.object(self.app.state.supervisor, "start",
                              return_value={"status": "RUNNING", "pid": 12345}) as started:
                reply = self.client.post(
                    "/api/launcher/codex/launch", headers=headers,
                    json={**request, "preview_hash": preview.json()["preview_hash"]},
                )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["pid"], 12345)
        started.assert_called_once()
        cfg = self.app.state.supervisor.lane_config("codex")
        self.assertEqual(cfg["launch_profile"], "codex-luna-high")
        self.assertEqual(cfg["prompt_override"], "Do not submit.")
        self.assertEqual(cfg["agent_name"], "Application Architect")
        self.assertFalse(cfg["auto_continue"])

    def test_model_selector_blocks_unknown_ids_and_invalid_efforts(self):
        token = self.client.get("/api/launcher/options").json()["token"]
        headers = {"X-MJobs-Launch-Token":token}
        base = {"profile":"codex-luna-high","effort":"high",
                "agent":"Application Architect","instructions":"","auto_continue":False}
        for model,effort in (("made-up-model","high"),("gpt-6-luna","ultra")):
            request = {**base,"model":model,"effort":effort}
            response = self.client.post("/api/launcher/codex/launch",headers=headers,json=request)
            self.assertEqual(response.status_code,409,response.text)
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())

    def test_agent_session_names_cannot_be_unverified_cli_flags(self):
        sup = self.app.state.supervisor
        for lane,slug,model in (
            ("antigravity","antigravity-opus","opus"),
            ("claude","claude-release-audit","sonnet"),
        ):
            sup.update_config(lane,{
                "prompt_file":"prompts/dual-agent/"+slug+".md",
                "launch_profile":slug,"model":model,
                "launch_workspace":str(self.root),
                "agent_name":"Custom Name 7"
            })
            args = sup._command(lane,self.root)
            self.assertNotIn("--agent",args)
            self.assertIn("Session label=Custom Name 7",args[2])

    def test_manual_model_refresh_requires_valid_session_token(self):
        forbidden = self.client.post("/api/launcher/models/refresh",json={})
        self.assertEqual(forbidden.status_code,403)
        token = self.client.get("/api/launcher/options").json()["token"]
        response = self.client.post("/api/launcher/models/refresh",
                                    headers={"X-MJobs-Launch-Token":token},json={})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["codex"]["models"][0]["id"],"gpt-6-luna")

    def test_antigravity_tier_model_omits_conflicting_effort_flag(self):
        sup = self.app.state.supervisor
        sup.update_config("antigravity",{
            "prompt_file":"prompts/dual-agent/antigravity-opus.md",
            "launch_profile":"antigravity-opus","model":"gemini-3.8-flash-medium",
            "reasoning":"medium","agent_name":"Scoring Specialist",
        })
        args = sup._command("antigravity",self.root)
        self.assertIn("gemini-3.8-flash-medium",args)
        self.assertNotIn("--effort",args)
        self.assertNotIn("--agent",args)

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

    def _codex_request(self):
        return {
            "profile": "codex-luna-high",
            "model": "gpt-6-luna",
            "effort": "high",
            "agent": "Application Architect",
            "instructions": "Use only test fixtures.",
            "auto_continue": False,
        }

    def _preview_request(self, request):
        token = self.client.get("/api/launcher/options").json()["token"]
        return self.client.post(
            "/api/launcher/codex/preview",
            headers={"X-MJobs-Launch-Token": token},
            json=request,
        )

    def test_preview_contains_actual_effective_prompt_and_cli_model_flags(self):
        request = self._codex_request()
        original_config = self.app.state.supervisor.lane_config("codex")
        response = self._preview_request(request)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("CURRENT EXPLICIT LAUNCH SELECTION: model=gpt-6-luna;", data["prompt"])
        self.assertIn("Session label=Application Architect", data["prompt"])
        self.assertIn("Use only test fixtures.", data["prompt"])
        self.assertIn("GOAL and verified implementation rules", data["prompt"])
        self.assertEqual(data["effort"], "high")
        self.assertEqual(data["model"], "gpt-6-luna")
        self.assertEqual(data["agent"], "Application Architect")
        self.assertIn("gpt-6-luna", data["command"])
        self.assertIn('model_reasoning_effort="high"', data["command"])
        self.assertIn("<approved prompt via stdin>", data["command"])
        self.assertIn("agents.max_concurrent_threads_per_session=50", data["command"])
        self.assertEqual(data["launch_mode"], "terminal")
        self.assertEqual(data["max_subagents"], 50)
        self.assertEqual(data["intended_subagents"], 8)
        self.assertEqual(len(data["preview_hash"]), 64)
        self.assertEqual(len(data["prompt_sha256"]), 64)
        self.assertEqual(self.app.state.supervisor.lane_config("codex"), original_config)
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())

    def test_all_launch_selection_changes_invalidate_preview_hash(self):
        original = self._preview_request(self._codex_request()).json()["preview_hash"]
        cases = [
            {"profile": "codex-sol-6-1-high"},
            {"model": "gpt-6.1-sol"},
            {"effort": "medium"},
            {"agent": "Another Engineer"},
            {"instructions": "Change approved output."},
            {"auto_continue": True},
        ]
        for change in cases:
            with self.subTest(change=change):
                request = {**self._codex_request(), **change}
                response = self._preview_request(request)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertNotEqual(data["preview_hash"], original)
                self.assertEqual(data["model"], request["model"])
                self.assertEqual(data["effort"], request["effort"])
                self.assertEqual(data["auto_continue"], request["auto_continue"])
                self.assertIn(str(request["model"]), data["prompt"])

    def test_preview_does_not_create_worktree_or_start_agent(self):
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())
        with patch.object(self.app.state.supervisor, "start",
                          side_effect=AssertionError("Preview attempted agent launch")):
            response = self._preview_request(self._codex_request())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())
        self.assertEqual(Path(response.json()["workspace"]), self.root)

    def test_launch_missing_wrong_or_stale_prompt_is_rejected_before_worktree(self):
        token = self.client.get("/api/launcher/options").json()["token"]
        headers = {"X-MJobs-Launch-Token": token}
        request = self._codex_request()
        preview = self._preview_request(request).json()
        invalid = [
            request,
            {**request, "preview_hash": "f" * 64},
            {**request, "preview_hash": preview["preview_hash"],
             "instructions": "Changed after approval."},
            {**request, "preview_hash": preview["preview_hash"],
             "model": "gpt-6.1-sol"},
        ]
        for config in invalid:
            with self.subTest(config=config):
                with patch.object(self.app.state.supervisor, "start",
                                  side_effect=AssertionError("Should not spawn a CLI")):
                    response = self.client.post(
                        "/api/launcher/codex/launch",
                        headers=headers, json=config,
                    )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())

    def test_file_modification_after_preview_invalidates_prior_approval(self):
        request = self._codex_request()
        approved = self._preview_request(request).json()
        profile_path = self.root / "prompts/codex/codex-luna-high.md"
        profile_path.write_text("Updated canonical prompt file.\n", encoding="utf-8")
        after = self._preview_request(request).json()
        self.assertNotEqual(approved["preview_hash"], after["preview_hash"])
        token = self.client.get("/api/launcher/options").json()["token"]
        with patch.object(self.app.state.supervisor, "start",
                          side_effect=AssertionError("Should not launch")):
            response = self.client.post(
                "/api/launcher/codex/launch",
                headers={"X-MJobs-Launch-Token": token},
                json={**request, "preview_hash": approved["preview_hash"]},
            )
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())

    def test_prompt_guard_rejects_last_second_mismatch_before_spawning(self):
        sup = self.app.state.supervisor
        sup.update_config("codex", {
            "launch_profile": "codex-luna-high",
            "launch_workspace": str(self.root),
            "prompt_file": "prompts/codex/codex-luna-high.md",
            "model": "gpt-6-luna",
            "reasoning": "high",
            "auto_continue": False,
        })
        with patch.object(sup, "_launch_command",
                          side_effect=AssertionError("Should not spawn CLI")):
            with self.assertRaisesRegex(RuntimeError, "Prompt changed after review"):
                sup.start("codex", manual=True, expected_prompt_hash="0" * 64)

    def test_preview_requires_same_authorization_as_launch(self):
        request = self._codex_request()
        missing = self.client.post("/api/launcher/codex/preview", json=request)
        self.assertEqual(missing.status_code, 403)
        token = self.client.get("/api/launcher/options").json()["token"]
        foreign = self.client.post(
            "/api/launcher/codex/preview", json=request,
            headers={"X-MJobs-Launch-Token": token, "Origin": "https://evil.example"},
        )
        self.assertEqual(foreign.status_code, 403)

    def test_reviewed_launch_mode_disables_legacy_control_bypasses(self):
        mission_app = create_app(self.root, mission_instance=True)
        client = TestClient(mission_app)
        health = client.get("/api/launcher/health").json()
        self.assertGreaterEqual(health["version"], 3)
        self.assertTrue(health["prompt_preview"])
        self.assertTrue(health["approval_required"])
        self.assertEqual(health["root"], str(self.root))
        self.assertTrue(client.get("/api/launcher/options").json()["mission_instance"])
        for endpoint in (
            "/api/lanes/codex/start", "/api/lanes/codex/stop",
            "/api/lanes/codex/restart", "/api/stop-all",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(client.post(endpoint, json={}).status_code, 403)
        self.assertEqual(client.patch(
            "/api/lanes/codex/config", json={"model": "gpt-6.1-sol"}
        ).status_code, 403)
        self.assertEqual(client.get("/api/launcher/options").status_code, 200)
        # No legacy bypass was allowed to create an isolated checkout.
        self.assertFalse((self.root.parent / "MJobs-Codex-App").exists())
    def test_visible_terminal_and_agent_capacity_are_exactly_previewed(self):
        request=self._codex_request()
        base=self._preview_request(request)
        self.assertEqual(base.status_code,200,base.text)
        first=base.json()
        self.assertEqual(first["launch_mode"],"terminal")
        self.assertEqual(first["max_subagents"],50)
        self.assertEqual(first["intended_subagents"],8)
        self.assertIn("agents.max_concurrent_threads_per_session=50",first["command"])
        self.assertIn("<approved prompt via stdin>",first["command"])
        for selection in (
            {"launch_mode":"headless"},
            {"max_subagents":16,"intended_subagents":8},
            {"max_subagents":20,"intended_subagents":16},
            {"intended_subagents":0},
            {"max_subagents":0,"intended_subagents":0},
        ):
            with self.subTest(selection=selection):
                response=self._preview_request({**request,**selection})
                self.assertEqual(response.status_code,200,response.text)
                choice=response.json()
                self.assertNotEqual(choice["preview_hash"],first["preview_hash"])
                self.assertEqual(choice["launch_mode"],selection.get("launch_mode","terminal"))
                self.assertEqual(choice["intended_subagents"],selection.get("intended_subagents",8))
                if selection.get("intended_subagents",8)==0:
                    self.assertIn("features.multi_agent=false",choice["command"])
                elif "max_subagents" in selection:
                    self.assertIn(
                        "agents.max_concurrent_threads_per_session="+str(selection["max_subagents"]),
                        choice["command"],
                    )

    def test_staffing_policy_cannot_exceed_maximum_or_reuse_old_approval(self):
        request=self._codex_request()
        approved=self._preview_request({**request,"max_subagents":12,
                                        "intended_subagents":8}).json()
        token=self.client.get("/api/launcher/options").json()["token"]
        for invalid in (
            {"max_subagents":51,"intended_subagents":8},
            {"max_subagents":-1,"intended_subagents":0},
            {"max_subagents":2,"intended_subagents":8},
            {"max_subagents":0,"intended_subagents":8},
        ):
            with self.subTest(invalid=invalid):
                response=self._preview_request({**request,**invalid})
                self.assertIn(response.status_code,(409,422))
        for changed in (
            {"max_subagents":16,"intended_subagents":8},
            {"max_subagents":12,"intended_subagents":4},
            {"max_subagents":12,"intended_subagents":8,"launch_mode":"headless"},
        ):
            with self.subTest(changed=changed),patch.object(
                self.app.state.supervisor,"start",
                side_effect=AssertionError("Stale approval must not spawn CLI")
            ):
                result=self.client.post("/api/launcher/codex/launch",
                    headers={"X-MJobs-Launch-Token":token},
                    json={**request,**changed,"preview_hash":approved["preview_hash"]},
                )
                self.assertEqual(result.status_code,409,result.text)
                self.assertFalse((self.root.parent/"MJobs-Codex-App").exists())

    def test_terminal_viewer_requires_authorization_but_not_a_new_agent(self):
        token=self.client.get("/api/launcher/options").json()["token"]
        self.assertEqual(self.client.post(
            "/api/launcher/codex/terminal-window",json={}
        ).status_code,403)
        self.assertEqual(self.client.post(
            "/api/launcher/codex/terminal-window",
            headers={"X-MJobs-Launch-Token":token},json={}
        ).status_code,409)
        log=self.app.state.supervisor.log_path("codex")
        log.write_text("previous safe log\n",encoding="utf-8")
        with patch.object(self.app.state.supervisor,"open_log_console",
                          return_value={"lane":"codex","pid":8008,"kind":"log_viewer",
                                        "reused":False}) as viewer, patch.object(
            self.app.state.supervisor,"start",
            side_effect=AssertionError("Viewer cannot start an agent")
        ):
            response=self.client.post("/api/launcher/codex/terminal-window",
                headers={"X-MJobs-Launch-Token":token},json={})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()["kind"],"log_viewer")
            viewer.assert_called_once_with("codex")



if __name__ == "__main__":
    unittest.main()
