"""Regression and compliance tests for unattended agent development governance."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from tools.verification.commit_provenance import validate as validate_provenance, AGENT_NAMES

ROOT = Path(__file__).resolve().parents[2]


class UnattendedAgentGovernanceTests(unittest.TestCase):
    def test_agents_contract_includes_antigravity_and_permission_modes(self):
        agents_md = ROOT / "AGENTS.md"
        self.assertTrue(agents_md.is_file(), "AGENTS.md must exist")
        content = agents_md.read_text(encoding="utf-8")

        # Harness support
        self.assertIn("Codex CLI", content)
        self.assertIn("Antigravity CLI", content)
        self.assertIn("`agy`", content)

        # Permission mode and preauthorization
        self.assertIn("Bypass Permissions", content)
        self.assertIn("Accept Edits", content)
        self.assertIn("preauthorized", content)
        self.assertIn("docs/UNATTENDED_AGENT_DEVELOPMENT.md", content)

        # Safety invariants preserved
        self.assertIn("Real external application submission stays **OFF**", content)
        self.assertIn("Protect this workstation", content)
        self.assertIn("AI-Agent: Antigravity", content)

    def test_unattended_policy_document_structure_and_rules(self):
        policy_path = ROOT / "docs" / "UNATTENDED_AGENT_DEVELOPMENT.md"
        self.assertTrue(policy_path.is_file(), "docs/UNATTENDED_AGENT_DEVELOPMENT.md must exist")
        text = policy_path.read_text(encoding="utf-8")

        # Must define the 8 permanent governance rules
        self.assertIn("Multi-Harness Support", text)
        self.assertIn("Acceptance of User-Selected Permission Modes", text)
        self.assertIn("Permission Mode Is Not a Project Readiness Failure", text)
        self.assertIn("No Repeated Blanket Approvals for Authorized Sessions", text)
        self.assertIn("Preauthorized Routine Operations", text)
        self.assertIn("Separation of Runtime Configuration from Repository Governance", text)
        self.assertIn("Non-Destructive Workflow, Ownership, and Auditability", text)
        self.assertIn("Compatibility with Codex Tooling and AI Attribution", text)

        # Inviolable safety boundaries
        self.assertIn("GitHub Protected Branches", text)
        self.assertIn("Required CI Checks", text)
        self.assertIn("External Job Submission Remains OFF", text)
        self.assertIn("No CAPTCHA, Anti-Bot, or Access-Control Bypass", text)
        self.assertIn("No Secret Exfiltration", text)
        self.assertIn("Host Workstation Protection", text)
        self.assertIn("No Fabrication of Evidence", text)

    def test_commit_provenance_supports_antigravity_and_gemini(self):
        self.assertIn("antigravity", AGENT_NAMES)
        self.assertIn("gemini", AGENT_NAMES)
        self.assertIn("agy", AGENT_NAMES)

        valid_antigravity_commit = """feat(governance): establish permanent unattended agent policy

AI-Agent: Antigravity
AI-Model: Gemini 3.8 Flash (High)
Requirement: housekeeping
Validation: pytest tests/unit/test_unattended_agent_governance.py PASS
UTC-Date: 2026-10-15T20:30:00Z
"""
        def fake_git(_root, *args):
            return valid_antigravity_commit if "--format=%B" in args else "Antigravity Agent"

        with patch("tools.verification.commit_provenance.changed_sources", return_value=["tools/governance.py"]), \
             patch("tools.verification.commit_provenance.git", side_effect=fake_git):
            success, message = validate_provenance(ROOT, "HEAD")

        self.assertTrue(success, message)
        self.assertIn("PASS: provenance complete", message)

    def test_commit_provenance_rejects_missing_trailers_on_antigravity_commit(self):
        def fake_git(_root, *args):
            return "feat: missing trailers" if "--format=%B" in args else "Antigravity Agent"

        with patch("tools.verification.commit_provenance.changed_sources", return_value=["tools/governance.py"]), \
             patch("tools.verification.commit_provenance.git", side_effect=fake_git):
            success, message = validate_provenance(ROOT, "HEAD")

        self.assertFalse(success)
        self.assertIn("FAIL: missing commit trailers", message)


if __name__ == "__main__":
    unittest.main()
