"""Contract tests for the local live-feedback task catalogue."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_PATH = ROOT / "cli" / "feedback_tasks.json"

EXPECTED_IDS = {
    f"{number:02d}-{slug}"
    for number, slug in [
        (1, "session-start"),
        (2, "quick-feedback"),
        (3, "bug-report"),
        (4, "missing-feature"),
        (5, "workflow-improvement"),
        (6, "redesign"),
        (7, "remove-or-simplify"),
        (8, "data-quality"),
        (9, "safety-concern"),
        (10, "visual-accessibility"),
        (11, "performance"),
        (12, "triage"),
        (13, "process-feedback"),
        (14, "retest"),
        (15, "session-handoff"),
        (16, "approval-and-reconcile"),
        (17, "launch-diagnosis"),
        (18, "capture-diagnosis"),
        (19, "usability-checkpoint"),
        (20, "core-workflow-review"),
    ]
}


class FeedbackTaskCatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        cls.by_id = {task["id"]: task for task in cls.tasks}

    def test_catalogue_covers_all_template_purposes_once(self):
        self.assertEqual(set(self.by_id), EXPECTED_IDS)
        self.assertEqual(len(self.tasks), len(EXPECTED_IDS))

    def test_task_and_field_schema_is_ui_safe(self):
        allowed_kinds = {"text", "textarea", "select"}
        for task in self.tasks:
            self.assertIsInstance(task["title"], str)
            self.assertIsInstance(task["group"], str)
            self.assertTrue(task["description"])
            self.assertGreaterEqual(len(task["fields"]), 1)
            required = [field for field in task["fields"] if field["required"]]
            self.assertGreaterEqual(len(required), 2, task["id"])
            self.assertLessEqual(len(required), 3, task["id"])
            field_ids = set()
            for field in task["fields"]:
                self.assertRegex(field["id"], r"^[a-z][a-z0-9_]*$")
                self.assertNotIn(field["id"], field_ids)
                field_ids.add(field["id"])
                self.assertIn(field["kind"], allowed_kinds)
                self.assertIsInstance(field["required"], bool)
                self.assertIsInstance(field["placeholder"], str)
                if field["kind"] == "select":
                    self.assertGreaterEqual(len(field.get("options", [])), 2)

    def test_high_risk_tasks_keep_their_safety_semantics(self):
        safety = self.by_id["09-safety-concern"]
        safety_fields = {field["id"] for field in safety["fields"]}
        self.assertTrue({"potential_risk", "last_safe_action", "observed_behavior"}.issubset(safety_fields))
        self.assertIn("submission_unconfirmed", safety["description"].lower())

        approval = self.by_id["16-approval-and-reconcile"]
        approval_fields = {field["id"] for field in approval["fields"]}
        self.assertTrue({"approval_stage", "artifact_version"}.issubset(approval_fields))
        stage = next(field for field in approval["fields"] if field["id"] == "approval_stage")
        self.assertEqual(stage["kind"], "select")
        self.assertIn("bounded design", " ".join(stage["options"]).lower())
        self.assertIn("exact", approval["description"].lower())
        self.assertIn("automatic", approval["description"].lower())

    def test_process_and_retest_tasks_bind_local_sessions(self):
        process_fields = {field["id"] for field in self.by_id["13-process-feedback"]["fields"]}
        self.assertTrue({"session_dir", "selected_finding", "requested_change"}.issubset(process_fields))

        retest = self.by_id["14-retest"]
        retest_fields = {field["id"] for field in retest["fields"]}
        self.assertTrue({"original_session", "retest_session", "decision"}.issubset(retest_fields))
        decision = next(field for field in retest["fields"] if field["id"] == "decision")
        self.assertEqual(decision["kind"], "select")
        self.assertEqual(set(decision["options"]), {"ACCEPT", "PARTIAL", "REJECT", "NOT_TESTED"})

    def test_guided_core_workflow_review_has_issue_ready_fields(self):
        task = self.by_id["20-core-workflow-review"]
        fields = {field["id"]: field for field in task["fields"]}
        self.assertTrue({"workflow", "outcome", "trying_to"}.issubset(fields))
        self.assertIn("Application tracking and follow-up", fields["workflow"]["options"])
        self.assertEqual(set(fields["outcome"]["options"]), {"PASS", "PARTIAL", "BLOCKED", "CONFUSING"})
        self.assertIn("granular", task["description"].lower())

    def test_session_start_captures_preflight_identity(self):
        task = self.by_id["01-session-start"]
        field_ids = {field["id"] for field in task["fields"]}
        self.assertTrue({"workspace", "branch", "service_url"}.issubset(field_ids))


if __name__ == "__main__":
    unittest.main()
