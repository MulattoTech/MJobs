from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.agent_dashboard.campaign_progress import campaign_progress
from tools.agent_dashboard.campaign_progress import _canonical_titles
from tools.agent_dashboard.server import create_app


ROOT = Path(__file__).resolve().parents[2]


def ledger(*, statuses: list[str] | None = None) -> dict:
    statuses = statuses or ["NOT_RUN"] * 60
    titles = _canonical_titles(ROOT)
    from collections import Counter
    counts = Counter(statuses)
    return {
        "schema_version": 1,
        "requirement_id": "MJ-REQ-292",
        "title": "Complete connected user journey",
        "updated_at_utc": "2026-11-06T12:00:00Z",
        "baseline_commit": "1" * 40,
        "scoreboard": {
            "total": 60,
            "verified_pass": counts["PASS"],
            "PARTIAL": counts["PARTIAL"],
            "NOT_RUN": counts["NOT_RUN"],
            "FAIL": counts["FAIL"],
            "BLOCKED": counts["BLOCKED"],
        },
        "steps": [
            {"number": number, "phase": next(phase for phase, start, end in
                                                  (("A", 1, 8), ("B", 9, 22), ("C", 23, 31),
                                                   ("D", 32, 35), ("E", 36, 53), ("F", 54, 60))
                                                  if start <= number <= end),
             "original_requirement": titles[number - 1], "status": statuses[number - 1],
             "missing_behavior": "Complete this numbered journey step with connected acceptance evidence.",
             "verification": {"tested_commit": None, "executed_at_utc": None,
                              "observed_result": None},
             "review": {"status": "NOT_RUN"}, "merge": {"status": "NOT_MERGED"}}
            for number in range(1, 61)
        ],
        "blockers": [],
        "next_action": "Run the first incomplete approved journey step.",
    }


class CampaignProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.campaign_root = Path(self.temp.name)
        ledger_path = self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json"
        ledger_path.parent.mkdir(parents=True)
        ledger_path.write_text(json.dumps(ledger()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_campaign_api_projects_only_user_journey_and_marks_ledger_claims(self):
        with patch.dict(os.environ, {"MJOBS_CAMPAIGN_ROOT": str(self.campaign_root)}):
            client = TestClient(create_app(ROOT))
            response = client.get("/api/progress/campaigns")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["available"])
        self.assertEqual(len(body["campaigns"]), 1)
        self.assertEqual(body["campaigns"][0]["requirement_id"], "MJ-REQ-292")
        self.assertNotIn("top_campaign", json.dumps(body).lower())
        campaign = body["campaigns"][0]
        self.assertEqual(campaign["acceptance_claim_scope"],
                         "recorded ledger; independent runtime and merge gates remain separate")
        self.assertEqual(campaign["scoreboard"]["total"], 60)
        self.assertEqual(campaign["scoreboard"]["verified_pass"], 0)
        self.assertEqual(campaign["phases"][0]["steps"][0]["title"], "Start the full stack.")
        self.assertEqual(campaign["ledger_sha256"], __import__("hashlib").sha256(
            (self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json").read_bytes()).hexdigest())
        self.assertEqual(len(body["requirements"]), body["overview"]["original"]["total"])
        self.assertTrue(all("title" in item for item in body["requirements"]))
        campaign_requirement = next(item for item in body["requirements"] if item["id"] == "MJ-REQ-292")
        self.assertEqual(campaign_requirement["issue"], 293)
        self.assertEqual(campaign_requirement["priority"], "P0")

    def test_malformed_missing_and_scoreboard_mismatch_are_unavailable_not_zero(self):
        path = self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json"
        malformed = (path.read_bytes())
        path.write_text("{bad", encoding="utf-8")
        response = campaign_progress(ROOT, configured_root=str(self.campaign_root))
        self.assertFalse(response["available"])
        self.assertEqual(response["campaigns"], [])
        self.assertNotIn("scoreboard", response)
        path.write_bytes(malformed)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["scoreboard"]["NOT_RUN"] = 59
        path.write_text(json.dumps(value), encoding="utf-8")
        response = campaign_progress(ROOT, configured_root=str(self.campaign_root))
        self.assertFalse(response["available"])
        self.assertEqual(response["campaigns"], [])
        path.unlink()
        response = campaign_progress(ROOT, configured_root=str(self.campaign_root))
        self.assertFalse(response["available"])

    def test_duplicate_and_missing_step_numbers_are_unavailable(self):
        path = self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json"
        value = ledger()
        value["steps"][1]["number"] = 1
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(campaign_progress(ROOT, configured_root=str(self.campaign_root))["available"])

    def test_stale_revisions_are_separate_and_reported_as_a_blocker(self):
        path = self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json"
        value = ledger()
        value["source_revision"] = "1" * 40
        path.write_text(json.dumps(value), encoding="utf-8")
        handoff = self.campaign_root / "planning" / "agent_handoffs" / "MJ-REQ-292.json"
        handoff.parent.mkdir(parents=True)
        handoff.write_text(json.dumps({"task": "MJ-REQ-292", "head_sha": "f" * 40}), encoding="utf-8")
        with patch("tools.agent_dashboard.campaign_progress._git_revision", return_value="2" * 40):
            response = campaign_progress(ROOT, configured_root=str(self.campaign_root))
        campaign = response["campaigns"][0]
        self.assertEqual(campaign["source_revision"], "2" * 40)
        self.assertEqual(campaign["handoff_revision"], "f" * 40)
        self.assertEqual(campaign["phases"][0]["steps"][0]["status"], "NOT_RUN")
        self.assertEqual(len(campaign["blockers"]), 2)
        self.assertTrue(all("revision differs" in blocker for blocker in campaign["blockers"]))

    def test_handoff_digest_is_checked_and_handoff_blockers_are_sanitized(self):
        import hashlib

        handoff = self.campaign_root / "planning" / "agent_handoffs" / "MJ-REQ-292.json"
        handoff.parent.mkdir(parents=True)
        ledger_path = self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json"
        value = {"task": "MJ-REQ-292", "acceptance_ledger_sha256": hashlib.sha256(
            ledger_path.read_bytes()).hexdigest(), "blockers": [
                "A useful checkpoint warning.", "Secret stored at C:\\private\\profile.env",
            ]}
        handoff.write_text(json.dumps(value), encoding="utf-8")
        campaign = campaign_progress(ROOT, configured_root=str(self.campaign_root))["campaigns"][0]
        self.assertTrue(campaign["ledger_hash_match"])
        self.assertIn("Recorded handoff: A useful checkpoint warning.", campaign["blockers"])
        self.assertFalse(any("private" in blocker for blocker in campaign["blockers"]))
        value["acceptance_ledger_sha256"] = "0" * 64
        handoff.write_text(json.dumps(value), encoding="utf-8")
        response = campaign_progress(ROOT, configured_root=str(self.campaign_root))
        self.assertFalse(response["available"])
        self.assertEqual(response["campaigns"], [])
        self.assertIn("requirements", response)

    def test_safe_long_prose_is_truncated_and_handoff_action_is_used(self):
        import hashlib

        path = self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json"
        value = ledger()
        value["steps"][0]["missing_behavior"] = "Remaining gap " * 30
        value["blockers"] = ["Ledger blocker " * 30]
        path.write_text(json.dumps(value), encoding="utf-8")
        handoff = self.campaign_root / "planning" / "agent_handoffs" / "MJ-REQ-292.json"
        handoff.parent.mkdir(parents=True)
        handoff.write_text(json.dumps({
            "task_id": "MJ-REQ-292",
            "acceptance_ledger_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "next_action": "Recorded next action " * 30,
            "blockers": ["Handoff blocker " * 30],
        }), encoding="utf-8")
        campaign = campaign_progress(ROOT, configured_root=str(self.campaign_root))["campaigns"][0]
        step = campaign["phases"][0]["steps"][0]
        self.assertTrue(step["remaining_gap"].startswith("Remaining gap"))
        self.assertEqual(len(step["remaining_gap"]), 240)
        self.assertTrue(step["remaining_gap"].endswith("…"))
        self.assertTrue(campaign["next_action"].startswith("Recorded next action"))
        self.assertTrue(campaign["next_action"].endswith("…"))
        self.assertTrue(any(blocker.startswith("Recorded handoff: Handoff blocker")
                            and blocker.endswith("…") for blocker in campaign["blockers"]))

    def test_configured_source_precedence_and_invalid_local_config_fail_closed(self):
        local_app_data = self.campaign_root / "local"
        config = local_app_data / "MJobs" / "MissionControl" / "campaign-source.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"schema_version": 1, "root": str(self.campaign_root)}), encoding="utf-8")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False):
            response = campaign_progress(ROOT)
        self.assertTrue(response["available"])
        self.assertEqual(response["campaigns"][0]["source_kind"], "working_checkpoint")
        config.write_text(json.dumps({"schema_version": 2, "root": str(self.campaign_root)}), encoding="utf-8")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False):
            response = campaign_progress(ROOT)
        self.assertFalse(response["available"])
        self.assertEqual(response["error"], "Campaign source configuration is invalid.")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data),
                                     "MJOBS_CAMPAIGN_ROOT": str(self.campaign_root)}, clear=False):
            response = campaign_progress(ROOT)
        self.assertTrue(response["available"])

    def test_private_or_command_like_checkpoint_text_is_not_returned(self):
        path = self.campaign_root / "planning" / "acceptance" / "MJ-REQ-292.json"
        value = ledger()
        value["steps"][0]["missing_behavior"] = "powershell remove-item C:\\private\\profile"
        value["steps"][0]["verification"]["observed_result"] = "git status --short"
        path.write_text(json.dumps(value), encoding="utf-8")
        response = campaign_progress(ROOT, configured_root=str(self.campaign_root))
        self.assertTrue(response["available"])
        step = response["campaigns"][0]["phases"][0]["steps"][0]
        self.assertNotIn("observed_result", step)
        self.assertNotIn("remaining_gap", step)
        self.assertNotIn("private", json.dumps(response).lower())


if __name__ == "__main__":
    unittest.main()
