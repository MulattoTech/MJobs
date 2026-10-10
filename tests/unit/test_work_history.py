"""Development history: truthful file classification, auto-updating and zero agent calls."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.agent_dashboard.work_history import (
    WorkHistory, classify_commit, parse_git_log, path_type, project_area, session_activity,
)
from tools.agent_dashboard.server import create_app


def run(root: Path, *args: str):
    cmd = ["git", "-C", str(root), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


class WorkHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "MJobs"
        self.root.mkdir()
        run(self.root, "init")
        run(self.root, "checkout", "-b", "main")
        run(self.root, "config", "user.name", "Read Only Fixture")
        run(self.root, "config", "user.email", "fixture@example.invalid")
        self.put("AGENTS.md", "# MJobs fixture\n")
        self.commit("docs: baseline instructions")

    def put(self, path: str, content: str):
        output = self.root / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")

    def commit(self, subject: str):
        run(self.root, "add", "-A")
        run(self.root, "commit", "-m", subject)

    def test_path_classification_distinguishes_product_docs_from_runtime(self):
        self.assertEqual(path_type("services/applications/adapter.py"), "PRODUCT")
        self.assertEqual(path_type("services/applications/AREA.md"), "DOCUMENTATION")
        self.assertEqual(path_type("tools/agent_dashboard/static/mission.js"), "PRODUCT")
        self.assertEqual(path_type("tests/unit/test_feature.py"), "TEST")
        self.assertEqual(path_type("docs/evidence/receipt.json"), "BOOKKEEPING")
        self.assertEqual(path_type("planning/PR_QUEUE.json"), "BOOKKEEPING")
        self.assertEqual(path_type("docs/DEVELOPER_GUIDE.md"), "DOCUMENTATION")
        self.assertEqual(path_type("tools/coordination/lane.py"), "OPERATIONS")

    def test_core_feature_requires_multiple_product_components(self):
        files = [
            {"path": "apps/web/src/main.tsx", "additions": 95, "deletions": 1},
            {"path": "apps/api/main.py", "additions": 60, "deletions": 5},
        ]
        self.assertEqual(classify_commit("feat: connected job matching", files)[0], "CORE_FEATURE")
        self.assertEqual(classify_commit("fix: reconcile bug", files)[0], "BUGFIX")
        self.assertEqual(classify_commit("docs: notes and code", files)[0], "PRODUCT_CHANGE")
        small = [{"path": "services/matching/weights.py", "additions": 10, "deletions": 2}]
        self.assertEqual(classify_commit("feat: add matching score", small)[0], "PRODUCT_FEATURE")
        self.assertEqual(project_area(["services/applications/state.py"]), "Applications")

    def test_live_git_commit_history_and_summary_are_read_only(self):
        self.put("apps/web/src/main.tsx", "export const panel = 'Ready';\n" * 90)
        self.put("apps/api/main.py", "def status(): return 'READY'\n" * 60)
        self.commit("feat: connected job search #125")
        self.put("tests/unit/test_workflow.py", "def test_present(): assert True\n")
        self.commit("test: acceptance after feature")
        self.put("planning/PR_QUEUE.json", '{"status":"PARTIAL"}\n')
        self.commit("docs(progress): record verification checkpoint")
        reader = WorkHistory(self.root, codex_home=self.root / "missing")
        before = run(self.root, "status", "--porcelain=v1")
        with patch("tools.agent_dashboard.work_history.session_activity",
                   return_value={"available":False}):
            result = reader.snapshot(limit=50)
        self.assertTrue(result["available"])
        self.assertEqual(result["sample_size"], 4)
        self.assertEqual(result["commits"][0]["category"], "BOOKKEEPING")
        self.assertEqual(result["commits"][1]["category"], "TEST")
        self.assertEqual(result["commits"][2]["category"], "CORE_FEATURE")
        self.assertEqual(result["commits"][2]["issue_refs"], ["#125"])
        self.assertEqual(result["commits"][2]["files_changed"], 2)
        self.assertEqual(result["commits"][2]["area"], "Web interface")
        self.assertEqual(result["categories"]["CORE_FEATURE"], 1)
        self.assertEqual(result["code_commits"], 1)
        self.assertEqual(run(self.root, "status", "--porcelain=v1"), before)
        self.assertFalse((self.root / "outputs").exists())
        self.assertFalse((self.root / "planning/lanes").exists())

    def test_cache_avoids_repeat_scans_and_refresh_bypasses_cache(self):
        self.put("services/discovery/a.py", "value=1\n")
        self.commit("feat: first source")
        reader = WorkHistory(self.root, codex_home=self.root / "none")
        with patch("tools.agent_dashboard.work_history.session_activity",
                   return_value={"available":False}), \
             patch("tools.agent_dashboard.work_history.run_git",
                   wraps=__import__("tools.agent_dashboard.work_history", fromlist=["run_git"]).run_git) as runner:
            first = reader.snapshot(limit=2)
            calls = runner.call_count
            again = reader.snapshot(limit=1)
            self.assertEqual(runner.call_count, calls)
            self.assertEqual(again["head"], first["head"])
            self.put("services/discovery/b.py", "value=2\n")
            self.commit("feat: second source")
            still = reader.snapshot(limit=5)
            self.assertEqual(still["head"], first["head"])
            forced = reader.snapshot(limit=5, force=True)
            self.assertNotEqual(forced["head"], first["head"])
            self.assertEqual(forced["commits"][0]["category"], "PRODUCT_FEATURE")

    def test_bounded_parser_rejects_malformed_nondigits_and_handles_merge(self):
        sha="a" * 40
        raw = (
            "__MJH__"+sha+"\x1f2026-10-10T10:02:00-05:00\x1fFixture\x1f"
            "feat: import private binary\n\n-\t-\tservices/documents/model.bin\n" +
            "__MJH__"+("b"*40)+"\x1f2026-10-10T10:01:00-05:00\x1fFixture"
            "\x1fmerge: summary\n"
        )
        parsed = parse_git_log(raw)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["files"][0]["binary"], True)
        self.assertEqual(parsed[0]["category"], "PRODUCT_FEATURE")
        self.assertEqual(parsed[1]["category"], "OTHER")

    def test_codex_turns_are_separate_metadata_not_commit_authorship(self):
        now=datetime(2026,10,10,11,0,tzinfo=timezone.utc)
        sessions = self.root / "fake-home"
        folder=sessions / "sessions" / "2026" / "10" / "10"
        folder.mkdir(parents=True)
        testfile=folder/"rollout-2026-10-10T11-00-00-fake.jsonl"
        lines=[
            {"type":"session_meta","timestamp":"2026-10-10T11:00:00Z",
             "payload":{"id":"fixture-root","cwd":str(self.root),"source":"cli"}},
            {"type":"turn_context","timestamp":"2026-10-10T11:00:20Z",
             "payload":{"model":"gpt-6-luna","effort":"high"}},
            {"type":"event_msg","timestamp":"2026-10-10T11:01:00Z",
             "payload":{"type":"token_count"}},
        ]
        testfile.write_text("\n".join(json.dumps(x) for x in lines)+"\n",encoding="utf-8")
        result=session_activity(self.root,codex_home=sessions,now=now)
        self.assertTrue(result["available"])
        self.assertEqual(result["last_turn_at"], "2026-10-10T11:00:20Z")
        self.assertEqual(result["model"],"gpt-6-luna")
        self.assertEqual(result["effort"],"high")
        self.assertIn("No per-commit model attribution",result["note"])
        self.assertNotIn("message",result)

    def test_history_api_is_read_only_and_validates_limit(self):
        self.put("apps/web/src/feature.tsx", "export const ready=true\n")
        self.commit("feat: user-visible UI")
        from tools.agent_dashboard.model_catalog import ModelCatalog
        from tools.agent_dashboard.model_catalog import _choice
        with patch.object(ModelCatalog, "_discover", return_value=(
            [_choice("gpt-6-luna","Luna",["high"],"high")], "fixture",
        )):
            app=create_app(self.root, mission_instance=True)
        client=TestClient(app)
        with patch("tools.agent_dashboard.work_history.session_activity",
                   return_value={"available":False}), \
             patch.object(app.state.supervisor, "start",
                          side_effect=AssertionError("History started an agent")):
            response=client.get("/api/history?limit=2")
            self.assertEqual(response.status_code,200,response.text)
            data=response.json()
            self.assertEqual(data["commits"][0]["category"],"PRODUCT_FEATURE")
            self.assertEqual(data["commits"][0]["subject"],"feat: user-visible UI")
            self.assertEqual(len(data["commits"]),2)
            self.assertEqual(client.get("/api/history?limit=0").status_code,422)
            self.assertEqual(client.get("/api/history?limit=101").status_code,422)
            self.assertEqual(client.post("/api/history").status_code,405)


if __name__=="__main__":
    unittest.main()
