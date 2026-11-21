"""Offline, no-credential tests for the autonomous GitHub PR triage policy."""
import importlib.util
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "ci" / "pr_backlog_triage.py"


def loaded():
    spec = importlib.util.spec_from_file_location("pr_backlog_triage_tested", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {
        "GITHUB_REPOSITORY": "MulattoTech/MJobs",
        "GITHUB_TOKEN": "synthetic-test-token"
    }):
        spec.loader.exec_module(module)
    return module


class BacklogTriageTests(TestCase):
    def test_module_import_never_issues_network_call(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network call")):
            self.assertTrue(callable(loaded().classify))

    def test_missing_checks_cannot_qualify_for_auto_merge(self):
        m = loaded()
        with patch.object(m, "get", side_effect=[
            {"check_runs": []}, {"statuses": []}
        ]):
            self.assertEqual(m.checks("a" * 40), (False, False))

    def test_failing_check_is_not_ready(self):
        m = loaded()
        with patch.object(m, "get", side_effect=[
            {"check_runs": [{"status": "completed", "conclusion": "failure"}]},
            {"statuses": []}
        ]):
            self.assertEqual(m.checks("a" * 40), (False, True))

    def test_old_commit_stays_stale_despite_label_updates(self):
        m = loaded()
        pr = {"head": {"sha": "a" * 40}, "updated_at": "2099-01-01T00:00:00Z"}
        with patch.object(m, "get", return_value={
            "commit": {"committer": {"date": "2025-01-01T00:00:00Z"}}
        }):
            self.assertGreaterEqual(m.code_age_days(pr), 30)

    def test_unknown_commit_age_fails_closed(self):
        m = loaded()
        with patch.object(m, "get", side_effect=RuntimeError("HTTP 403")):
            self.assertEqual(m.code_age_days({"head": {"sha": "a" * 40}}), 365)

    def test_security_or_policy_docs_never_auto_eligible(self):
        m = loaded()
        pr = {"number": 123, "user": {"login": "MulattoTech"},
              "changed_files": 1, "additions": 10, "deletions": 0}
        with patch.object(m, "get", return_value=[
            {"filename": "docs/security/authorization.md"}
        ]):
            self.assertFalse(m.docs_only(pr))
        with patch.object(m, "get", return_value=[
            {"filename": "docs/release-notes.md"}
        ]):
            self.assertTrue(m.docs_only(pr))
