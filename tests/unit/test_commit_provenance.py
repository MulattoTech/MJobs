"""Deterministic provenance validator unit tests (no network)."""
from __future__ import annotations
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.verification.commit_provenance import validate

VALID = """chore: integration

AI-Agent: ChatGPT-Web
AI-Model: GPT-6
Requirement: housekeeping
Validation: pytest PASS; hosted NOT_RUN
UTC-Date: 2026-10-11T04:00:00Z
"""


class CommitProvenanceTests(unittest.TestCase):
    def fake_git(self, body, author):
        def result(_root, *args):
            return body if "--format=%B" in args else author
        return result

    def test_ai_source_commit_has_all_trailers(self):
        with patch("tools.verification.commit_provenance.changed_sources",
                   return_value=["tools/example.py"]), patch(
                       "tools.verification.commit_provenance.git",
                       side_effect=self.fake_git(VALID,"ChatGPT Web")):
            success, message = validate(Path("."), "HEAD")
        self.assertTrue(success, message)

    def test_missing_provenance_fails_for_ai(self):
        with patch("tools.verification.commit_provenance.changed_sources",
                   return_value=["apps/web/src/example.tsx"]), patch(
                       "tools.verification.commit_provenance.git",
                       side_effect=self.fake_git("feat: incomplete", "Codex")):
            success, message = validate(Path("."), "HEAD")
        self.assertFalse(success)
        self.assertIn("missing commit trailers", message)

    def test_human_commit_can_skip(self):
        with patch("tools.verification.commit_provenance.changed_sources",
                   return_value=["services/example.py"]), patch(
                       "tools.verification.commit_provenance.git",
                       side_effect=self.fake_git("feat: human change","Developer")):
            success, _ = validate(Path("."), "HEAD")
        self.assertTrue(success)

    def test_strict_mode_requires_trailers(self):
        with patch("tools.verification.commit_provenance.changed_sources",
                   return_value=["services/example.py"]), patch(
                       "tools.verification.commit_provenance.git",
                       side_effect=self.fake_git("feat: no trailers","Developer")):
            success, _ = validate(Path("."), "HEAD", strict=True)
        self.assertFalse(success)

    def test_doc_only_change_exempt(self):
        with patch("tools.verification.commit_provenance.changed_sources",
                   return_value=[]):
            success, _ = validate(Path("."), "HEAD", strict=True)
        self.assertTrue(success)


if __name__=="__main__":
    unittest.main()
