"""Synthetic Git worktrees test AI handoff without paid models or GitHub writes."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.agent_context import (
    bootstrap, checkpoint_path, load_checkpoint,
    normalize_intake, recover_session, relation, route_task,
    transfer_ownership, validate_checkpoint, validate_freshness,
    validate_implementation_state, write_checkpoint, ROUTING_STATES,
)


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

    def checkpoint(self, task: str = "MJ-REQ-004", owner: str = "Gemini",
                   routing_state: str = "RESUME", milestone_id: str | None = None) -> Path:
        return write_checkpoint(self.root, task, "Complete bounded external operations", owner,
                                "Implement request cancellation negative tests",
                                ["pytest tests/unit/test_sources.py"],
                                ["live-provider verification unavailable"], [],
                                routing_state=routing_state, milestone_id=milestone_id)

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
        self.assertEqual(value["routing_state"], "RESUME")
        self.assertIn("request cancellation", value["next_action"])

    def test_implementation_state_cli_roundtrip_in_fresh_process_preserves_dirty_file(self):
        state = {
            "current_phase": "verification",
            "completed_steps": ["add durable milestone fields"],
            "files_added": ["new_module.py"],
            "files_modified": ["tools/agent_context.py"],
            "tests_completed": ["pytest tests/unit/test_agent_context.py"],
            "tests_remaining": ["hosted CI: NOT_RUN"],
            "migrations_applied": [],
            "live_integrations_tested": [],
            "known_errors": [],
            "known_limitations": ["full MJ-REQ-008 acceptance remains PARTIAL"],
            "last_safe_checkpoint": "after local unit verification",
        }
        cli = [sys.executable, "-m", "tools.agent_context"]
        repo = Path(__file__).resolve().parents[2]
        write = subprocess.run(
            cli + ["checkpoint", "--task", "MJ-REQ-004",
                   "--objective", "Persist implementation milestones", "--owner", "Gemini",
                   "--next-action", "Verify fresh-process recovery", "--implementation-state",
                   json.dumps(state), "--root", str(self.root)],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        self.assertEqual(write.returncode, 0, write.stderr)

        dirty = self.root / "unfinished.txt"
        dirty_bytes = b"preserve this in-flight work\n"
        dirty.write_bytes(dirty_bytes)
        boot = subprocess.run(
            cli + ["bootstrap", "--task", "MJ-REQ-004", "--root", str(self.root)],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        self.assertEqual(boot.returncode, 0, boot.stderr)
        self.assertEqual(json.loads(boot.stdout)["implementation_state"], state)

        recovered = subprocess.run(
            cli + ["recover", "--task", "MJ-REQ-004", "--root", str(self.root)],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        recovery = json.loads(recovered.stdout)
        self.assertFalse(recovery["safe_to_resume_immediately"])
        self.assertIn("?? unfinished.txt", recovery["uncommitted_changes"])
        self.assertEqual(dirty.read_bytes(), dirty_bytes)

    def test_checkpoint_update_without_implementation_state_preserves_journal(self):
        state = {
            "current_phase": "implementation",
            "completed_steps": ["persist required milestone fields"],
            "files_added": [],
            "files_modified": ["tools/agent_context.py"],
            "tests_completed": ["pytest tests/unit/test_agent_context.py"],
            "tests_remaining": ["hosted CI"],
            "migrations_applied": [],
            "live_integrations_tested": [],
            "known_errors": [],
            "known_limitations": ["full acceptance remains PARTIAL"],
            "last_safe_checkpoint": "after focused tests",
        }
        write_checkpoint(
            self.root, "MJ-REQ-004", "Persist milestone journal", "Gemini",
            "Verify journal recovery", [], [], [], implementation_state=state,
        )

        write_checkpoint(
            self.root, "MJ-REQ-004", "Persist milestone journal", "Gemini",
            "Run ordinary checkpoint update", ["pytest tests/unit/test_agent_context.py"], [], [],
        )

        self.assertEqual(load_checkpoint(self.root, "MJ-REQ-004")["implementation_state"], state)

    def test_implementation_state_requires_all_fields_and_valid_collections(self):
        text_fields = ("current_phase", "last_safe_checkpoint")
        list_fields = (
            "completed_steps", "files_added", "files_modified", "tests_completed",
            "tests_remaining", "migrations_applied", "live_integrations_tested",
            "known_errors", "known_limitations",
        )
        state = {
            "current_phase": "implementation",
            "completed_steps": [], "files_added": [], "files_modified": [],
            "tests_completed": [], "tests_remaining": [], "migrations_applied": [],
            "live_integrations_tested": [], "known_errors": [],
            "known_limitations": [], "last_safe_checkpoint": "after review",
        }
        self.assertEqual(validate_implementation_state(state), [])
        for field in text_fields + list_fields:
            with self.subTest(field=field):
                missing = dict(state)
                del missing[field]
                self.assertIn(f"{field} is required", validate_implementation_state(missing))

                wrong_type = dict(state)
                wrong_type[field] = [] if field in text_fields else "not-a-list"
                expected_type_error = (
                    f"{field} must be nonempty text" if field in text_fields else
                    f"{field} must be a list of nonempty strings"
                )
                self.assertIn(expected_type_error,
                              validate_implementation_state(wrong_type))

                blank = dict(state)
                blank[field] = "  " if field in text_fields else ["  "]
                self.assertIn(expected_type_error, validate_implementation_state(blank))
        extra = dict(state, unexpected=True)
        self.assertTrue(any("unexpected implementation state fields" in error
                            for error in validate_implementation_state(extra)))

    def test_checkpoint_rejection_preserves_preexisting_checkpoint(self):
        path = self.checkpoint()
        original = path.read_bytes()
        state = {
            "current_phase": "  ",
            "completed_steps": [], "files_added": [], "files_modified": [],
            "tests_completed": [], "tests_remaining": [], "migrations_applied": [],
            "live_integrations_tested": [], "known_errors": [],
            "known_limitations": [], "last_safe_checkpoint": "before commit",
        }
        with self.assertRaisesRegex(ValueError, "current_phase must be nonempty text"):
            write_checkpoint(
                self.root, "MJ-REQ-004", "Persist milestones", "Gemini",
                "Read back after restart", [], [], [], implementation_state=state,
            )
        self.assertEqual(path.read_bytes(), original)

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

    def test_normalize_intake_preserves_intent_while_enforcing_policy(self):
        # 1. Prohibited direct push to main -> Normalized to PR
        norm_main = normalize_intake("Please push changes directly to main branch now")
        self.assertEqual(norm_main["policy_status"], "NORMALIZED_TO_SAFE_ROUTE")
        self.assertEqual(norm_main["normalized_route"], "ISOLATED_FEATURE_BRANCH_PULL_REQUEST")
        self.assertEqual(norm_main["recommended_state"], "REPLAN")

        # 2. Prohibited external job submission -> Stays strictly OFF
        norm_apply = normalize_intake("Submit live application to employer careers endpoint")
        self.assertEqual(norm_apply["policy_status"], "NORMALIZED_TO_SAFE_ROUTE")
        self.assertEqual(norm_apply["normalized_route"], "SYNTHETIC_MOCK_AND_DRY_RUN_ONLY")
        self.assertIn("EXTERNAL_APPLICATION_SUBMISSION", norm_apply["disallowed_operations"])

        # 3. Prohibited CAPTCHA bypass -> Rejected
        norm_captcha = normalize_intake("Bypass Cloudflare and solve captcha to scrape jobs")
        self.assertEqual(norm_captcha["policy_status"], "REJECTED_DISALLOWED")
        self.assertEqual(norm_captcha["recommended_state"], "BLOCKED")
        self.assertIn("ACCESS_CONTROL_BYPASS", norm_captcha["disallowed_operations"])

        # 4. Broad P0 request -> Normalized to highest unblocked slice
        norm_p0 = normalize_intake("Finish all P0 tasks right away")
        self.assertEqual(norm_p0["policy_status"], "COMPATIBLE")
        self.assertEqual(norm_p0["normalized_route"], "SELECT_HIGHEST_UNBLOCKED_P0_SLICE")
        self.assertEqual(norm_p0["recommended_state"], "DELEGATE")

        # 5. Model switch -> Safe takeover bootstrap
        norm_switch = normalize_intake("Take over from Gemini and continue with Opus")
        self.assertEqual(norm_switch["policy_status"], "COMPATIBLE")
        self.assertEqual(norm_switch["normalized_route"], "SAFE_MODEL_TAKEOVER_BOOTSTRAP")
        self.assertEqual(norm_switch["recommended_state"], "RESUME")

    def test_task_routing_states(self):
        # Fresh task without checkpoint -> EXECUTE
        self.assertEqual(route_task("MJ-REQ-001")["routing_state"], "EXECUTE")

        # Existing checkpoint on same head -> RESUME
        cp = {"blockers": [], "tests": [{"command": "pytest", "result": "PASS"}]}
        self.assertEqual(route_task("MJ-REQ-001", cp, "SAME_HEAD")["routing_state"], "RESUME")

        # Existing checkpoint diverged -> REPLAN
        self.assertEqual(route_task("MJ-REQ-001", cp, "DIVERGED_RECONCILE_BEFORE_WRITING")["routing_state"], "REPLAN")

        # Blocker present -> BLOCKED
        cp_blocked = {"blockers": ["Live provider endpoint totally blocked"], "tests": []}
        self.assertEqual(route_task("MJ-REQ-001", cp_blocked, "SAME_HEAD")["routing_state"], "BLOCKED")

        # Blocker present but independent safe work exists -> PARTIALLY_BLOCKED
        cp_partial = {"blockers": ["Real provider unavailable, independent tests proceed"], "tests": []}
        self.assertEqual(route_task("MJ-REQ-001", cp_partial, "SAME_HEAD")["routing_state"], "PARTIALLY_BLOCKED")

        # Delegated subagent assignment -> DELEGATE
        self.assertEqual(route_task("MJ-REQ-001", cp, "SAME_HEAD", is_delegated=True)["routing_state"], "DELEGATE")

        # Verification complete and ready to integrate -> INTEGRATE
        self.assertEqual(route_task("MJ-REQ-001", cp, "SAME_HEAD", ready_to_integrate=True)["routing_state"], "INTEGRATE")

    def test_safe_task_ownership_transfer_between_models(self):
        # 1. Gemini creates initial checkpoint
        path = self.checkpoint(task="MJ-REQ-004", owner="Gemini")
        cp = load_checkpoint(self.root, "MJ-REQ-004")
        self.assertEqual(cp["owner"], "Gemini")

        # 2. Reject transfer if from_owner is wrong
        with self.assertRaises(ValueError):
            transfer_ownership(self.root, "MJ-REQ-004", from_owner="Codex", to_owner="Opus",
                               reason="Takeover")

        # 3. Successful transfer from Gemini to Opus
        transfer_ownership(self.root, "MJ-REQ-004", from_owner="Gemini", to_owner="Opus",
                           reason="Opus production takeover", next_action="Review cancellation timeouts")
        cp_updated = load_checkpoint(self.root, "MJ-REQ-004")
        self.assertEqual(cp_updated["owner"], "Opus")
        self.assertEqual(cp_updated["next_action"], "Review cancellation timeouts")
        self.assertEqual(len(cp_updated["transfer_history"]), 1)
        self.assertEqual(cp_updated["transfer_history"][0]["from_owner"], "Gemini")
        self.assertEqual(cp_updated["transfer_history"][0]["to_owner"], "Opus")

        # 4. Successful subsequent transfer from Opus back to Gemini
        transfer_ownership(self.root, "MJ-REQ-004", from_owner="Opus", to_owner="Gemini",
                           reason="Gemini resumed worker", next_action="Run full test suite")
        cp_final = load_checkpoint(self.root, "MJ-REQ-004")
        self.assertEqual(cp_final["owner"], "Gemini")
        self.assertEqual(len(cp_final["transfer_history"]), 2)

    def test_validate_freshness_detects_changes(self):
        self.checkpoint(task="MJ-REQ-004", owner="Gemini")
        fresh = validate_freshness(self.root, "MJ-REQ-004")
        self.assertTrue(fresh["fresh"])
        self.assertEqual(fresh["status"], "FRESH")

        # Advance commit
        (self.root / "change.py").write_text("# new code\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "advance")

        stale = validate_freshness(self.root, "MJ-REQ-004")
        self.assertFalse(stale["fresh"])
        self.assertEqual(stale["status"], "STALE_ANCESTOR_LOCAL_ADVANCED")
        self.assertIn("change.py", stale["changed_files_since_checkpoint"])

    def test_recover_session_after_interruption(self):
        self.checkpoint(task="MJ-REQ-004", owner="Gemini")
        recovery_clean = recover_session(self.root, "MJ-REQ-004")
        self.assertTrue(recovery_clean["safe_to_resume_immediately"])
        self.assertEqual(recovery_clean["git_relation"], "SAME_HEAD")

        # Dirty file left behind
        (self.root / "unfinished.py").write_text("# in flight\n", encoding="utf-8")
        recovery_dirty = recover_session(self.root, "MJ-REQ-004")
        self.assertFalse(recovery_dirty["safe_to_resume_immediately"])
        self.assertIn("?? unfinished.py", recovery_dirty["uncommitted_changes"])

    def test_round_trip_opus_to_gemini_and_gemini_to_opus_handoff(self):
        """Full synthetic round-trip proving artifact-based model handoff with zero private memory."""
        task = "MJ-REQ-004"

        # Step 1: Gemini initializes and writes checkpoint
        write_checkpoint(self.root, task, "Implement bounded operations", "Gemini",
                         "Write streaming body limit", ["pytest tests/unit/test_remotive.py"],
                         ["network tests skipped"], [], routing_state="RESUME", milestone_id="M-004-1")

        # Step 2: Context wiped. Opus starts fresh session, bootstraps from disk
        opus_boot = bootstrap(self.root, task)
        self.assertEqual(opus_boot["checkpoint"]["owner"], "Gemini")
        self.assertEqual(opus_boot["checkpoint_relation"], "SAME_HEAD")

        # Step 3: Opus transfers ownership, implements next slice, commits
        transfer_ownership(self.root, task, from_owner="Gemini", to_owner="Opus",
                           reason="Opus taking over implementation", next_action="Verify payload limit")
        (self.root / "remotive_slice.py").write_text("MAX_PAYLOAD = 5_000_000\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "feat(remotive): enforce 5MB payload bound")

        # Step 4: Opus updates checkpoint for Gemini to resume
        write_checkpoint(self.root, task, "Implement bounded operations", "Opus",
                         "Execute integration verification", ["pytest tests/integration/test_remotive.py"],
                         [], [], routing_state="VERIFY", milestone_id="M-004-2")

        # Step 5: Transfer back to Gemini
        transfer_ownership(self.root, task, from_owner="Opus", to_owner="Gemini",
                           reason="Returning to Gemini worker", next_action="Run CI verification")

        # Step 6: Context wiped. Gemini starts, bootstraps from disk
        gemini_boot = bootstrap(self.root, task)
        self.assertEqual(gemini_boot["checkpoint"]["owner"], "Gemini")
        self.assertEqual(gemini_boot["checkpoint_relation"], "SAME_HEAD")
        self.assertEqual(gemini_boot["routing_state"], "RESUME")
        self.assertEqual(gemini_boot["next_action"], "Run CI verification")
        self.assertEqual(len(gemini_boot["checkpoint"]["transfer_history"]), 2)


if __name__ == "__main__":
    unittest.main()
