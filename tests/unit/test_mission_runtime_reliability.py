"""Regression tests: root .venv reuse, honest run states, readable Codex logs.

These tests never invoke Codex, call a model, install packages or alter the
MJobs application checkout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.agent_dashboard.agent_environment import (
    build_subprocess_environment, resolve_python_environment,
)
from tools.agent_dashboard.console_presentation import LiveDisplay, latest_run_excerpt
from tools.agent_dashboard.supervisor import AgentSupervisor, classify_exit


class EnvironmentResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.work = Path(self.tmp.name) / "work"
        self.root.mkdir()
        self.work.mkdir()
        for folder in (self.root, self.work):
            (folder / "requirements.lock").write_text(
                "playwright==1.63.0\nSQLAlchemy==2.1.1\n", encoding="utf-8"
            )
        self.venv = self.root / ".venv"
        self.python = self.venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"synthetic executable marker only")

    def test_shared_verified_environment_selected_without_worktree_install(self):
        expected = {
            "prefix": str(self.venv),
            "missing": [],
        }
        with patch(
            "tools.agent_dashboard.agent_environment.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=json.dumps(expected)),
        ) as run:
            selected = resolve_python_environment(self.root, self.work)
        self.assertEqual(selected["source"], "MJobs primary checkout")
        self.assertEqual(selected["interpreter"], str(self.python))
        self.assertIn("playwright", selected["verified_modules"])
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.work / ".venv").exists())

    def test_missing_or_incompatible_modules_fail_before_launch(self):
        with patch(
            "tools.agent_dashboard.agent_environment.subprocess.run",
            return_value=MagicMock(
                returncode=0, stdout=json.dumps({
                    "prefix": str(self.venv), "missing": ["playwright", "sqlalchemy"],
                }),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Playwright and SQLAlchemy"):
                resolve_python_environment(self.root, self.work)

    def test_lock_file_mismatch_rejects_vanishing_dependency_guarantee(self):
        (self.work / "requirements.lock").write_text(
            "playwright==7.0.0\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "differs from main"):
            resolve_python_environment(self.root, self.work)

    def test_missing_environment_refuses_global_python_fallback(self):
        self.python.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            resolve_python_environment(self.root, self.work)

    def test_launch_environment_prepends_venv_without_modifying_global_env(self):
        source = {
            "Path": "C:\\Windows\\System32;C:\\Tools\\Other",
            "PYTHONHOME": "C:\\OldInterpreter",
            "UNRELATED": "keep",
        }
        current = dict(source)
        env = build_subprocess_environment(source, venv_dir=str(self.venv))
        self.assertEqual(source, current)
        self.assertEqual(env["VIRTUAL_ENV"], str(self.venv))
        self.assertEqual(env["MJOBS_PYTHON_INTERPRETER"], str(self.python))
        self.assertTrue(env["PATH"].startswith(str(self.python.parent)))
        self.assertEqual(env["PIP_REQUIRE_VIRTUALENV"], "1")
        self.assertEqual(env["UNRELATED"], "keep")
        self.assertNotIn("Path", env)
        self.assertNotIn("PYTHONHOME", env)


class HonestRunStatusTests(unittest.TestCase):
    def test_successful_partial_run_with_blocked_tests_is_not_a_blocked_cli(self):
        tail = (
            '+      "result": "BLOCKED_ENVIRONMENT",\n'
            '+      "detail": "Playwright unavailable in global interpreter"\n'
            'tokens used\n214,452\n'
            '**Status: PARTIAL.** Four commits preserved. '
            'Browser execution is **BLOCKED_ENVIRONMENT**.\n'
            '=== CLI EXIT 0 ===\n'
        )
        self.assertEqual(classify_exit(0, tail), "PARTIAL")

    def test_status_without_final_partial_is_completed_on_zero_exit(self):
        self.assertEqual(
            classify_exit(0, '+ "result": "BLOCKED_ENVIRONMENT"\n'
                         'tokens used\n10\nFeature completed normally.\n'),
            "COMPLETED",
        )
        self.assertEqual(classify_exit(1, "BLOCKED_ENVIRONMENT: missing dependencies"),
                         "BLOCKED_ENVIRONMENT")
        self.assertEqual(classify_exit(7, "Unexpected CLI crash"), "ERROR")
        self.assertEqual(classify_exit(0, "Do you approve this design?"),
                         "WAITING_APPROVAL")

    def test_stale_blocked_state_corrected_from_existing_completed_log(self):
        root = Path(tempfile.mkdtemp(prefix="mjobs-status-migration-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
        common = root / ".git" / "mjobs-coordination"
        common.mkdir(parents=True)
        out = root / "outputs" / "mission-control-v2"
        out.mkdir(parents=True)
        (out / "codex.log").write_text(
            '+ "result": "BLOCKED_ENVIRONMENT"\n'
            "tokens used\n123,000\n**Status: PARTIAL.** Some tests not run.\n"
            "=== CLI EXIT 0 ===\n",
            encoding="utf-8",
        )
        stale = {"schema_version": 1, "lanes": {
            "codex": {
                "pid": None, "status": "BLOCKED_ENVIRONMENT",
                "exit_code": 0, "last_reason": "BLOCKED_ENVIRONMENT",
            }
        }}
        (common / "mission-control-v2-runtime.json").write_text(
            json.dumps(stale), encoding="utf-8"
        )
        sup = AgentSupervisor(root, mission_instance=True)
        self.assertEqual(sup.runtime["lanes"]["codex"]["status"], "PARTIAL")
        self.assertEqual(sup.runtime["lanes"]["codex"]["last_reason"], "PARTIAL")


class ReadableLogTests(unittest.TestCase):
    SAMPLE = (
        "\n===== START 2026-10-10T18:32Z workspace=MJobs-Codex-App =====\n"
        "Codex exec started\n"
        "diff --git a/test.py b/test.py\n"
        "+++ b/test.py\n"
        "@@ -10,4 +10,8 @@\n"
        "+print('new feature')\n"
        "tokens used\n214,452\n"
        "**Status: PARTIAL.** Four commits preserved.\n"
        "Browser check unavailable; 12 unit tests passed.\n"
        "=== CLI EXIT 0 ===\n"
    )

    def test_viewer_shows_final_result_not_mid_diff(self):
        summary = latest_run_excerpt(self.SAMPLE)
        self.assertIn("Status: PARTIAL", summary)
        self.assertIn("12 unit tests passed", summary)
        self.assertNotIn("diff --git", summary)
        self.assertNotIn("+++ b/", summary)
        self.assertIn("CLI EXIT 0", summary)

    def test_windows_crlf_console_log_uses_final_summary_not_diff(self):
        summary = latest_run_excerpt(self.SAMPLE.replace("\n", "\r\n"))
        self.assertIn("LATEST CODEX RUN SUMMARY", summary)
        self.assertIn("Status: PARTIAL", summary)
        self.assertNotIn("diff --git", summary)

    def test_live_stream_keeps_codex_activity_but_omits_patch_lines(self):
        presenter = LiveDisplay()
        self.assertIn("exec", presenter.feed("exec\n"))
        content = presenter.feed(
            "diff --git a/x b/x\n@@ -1 +1 @@\n-1\n+2\n"
            "tokens used\n100\n**Status: PARTIAL.** Completed one slice.\n"
        )
        self.assertIn("Code patch generated", content)
        self.assertIn("Status: PARTIAL", content)
        self.assertNotIn("@@", content)
        self.assertNotIn("+2", content)

    def test_empty_and_in_progress_logs_have_coherent_headers(self):
        self.assertIn("No completed run", latest_run_excerpt(""))
        excerpt = latest_run_excerpt("\n===== START today =====\n"
                                     "diff --git a/a b/a\n+new-line\n"
                                     "exec\npython -m unittest tests.unit.foo\n")
        self.assertIn("RECENT CODING ACTIVITY", excerpt)
        self.assertNotIn("+new-line", excerpt)


if __name__ == "__main__":
    unittest.main()
