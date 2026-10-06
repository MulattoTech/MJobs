from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.verification.agent_skills import validate_agent_skills


SKILL = """---
name: mjobs-delivery
description: Test skill.
---

references/db-migration.md
references/source-adapter.md
references/application-adapter.md
references/privacy-security.md
references/llm-provider.md
references/release-verification.md
python -m tools.verification.reference_provenance
"""

PLAYBOOKS = [
    "references/db-migration.md",
    "references/source-adapter.md",
    "references/application-adapter.md",
    "references/privacy-security.md",
    "references/llm-provider.md",
    "references/release-verification.md",
]


class AgentSkillValidatorTests(unittest.TestCase):
    def make_root(self) -> Path:
        root = Path(tempfile.mkdtemp())
        skill = root / ".agents" / "skills" / "mjobs-delivery"
        (skill / "references").mkdir(parents=True)
        (root / "planning").mkdir()
        (root / "docs" / "evidence").mkdir(parents=True)
        (root / "tools" / "verification").mkdir(parents=True)
        (root / "AGENTS.md").write_text("contract", encoding="utf-8")
        (root / "planning" / "IMPLEMENTATION_STATE.json").write_text("{}", encoding="utf-8")
        (root / "planning" / "PR_QUEUE.json").write_text("{}", encoding="utf-8")
        for name in ("catalogue.py", "progress.py", "agent_skills.py"):
            (root / "tools" / "verification" / name).write_text("# test", encoding="utf-8")
        (root / "tools" / "verification" / "reference_provenance.py").write_text("# test", encoding="utf-8")
        (root / "docs" / "reference").mkdir()
        for name in ("REFERENCE_PROJECTS.json", "REFERENCE_IMPLEMENTATION_POLICY.md"):
            (root / "docs" / "reference" / name).write_text("# test", encoding="utf-8")
        (root / "THIRD_PARTY_NOTICES.md").write_text("# notices", encoding="utf-8")
        (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
        for relative in PLAYBOOKS:
            (skill / relative).write_text("# playbook", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "skill": "mjobs-delivery",
            "required_playbooks": PLAYBOOKS,
            "required_paths": [
                "AGENTS.md",
                "planning/IMPLEMENTATION_STATE.json",
                "planning/PR_QUEUE.json",
                "docs/evidence",
                "tools/verification/catalogue.py",
                "tools/verification/progress.py",
                "docs/reference/REFERENCE_PROJECTS.json",
                "docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md",
                "THIRD_PARTY_NOTICES.md",
            ],
            "command_modules": [
                "tools.verification.agent_skills",
                "tools.verification.catalogue",
                "tools.verification.progress",
                "tools.verification.reference_provenance",
            ],
        }
        (skill / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        return root

    def test_valid_skill_contract_passes(self):
        self.assertEqual(validate_agent_skills(self.make_root()), [])

    def test_missing_playbook_is_reported(self):
        root = self.make_root()
        (root / ".agents" / "skills" / "mjobs-delivery" / PLAYBOOKS[0]).unlink()
        self.assertIn(
            f"Missing project skill playbook: {PLAYBOOKS[0]}",
            validate_agent_skills(root),
        )

    def test_missing_referenced_command_module_is_reported(self):
        root = self.make_root()
        (root / "tools" / "verification" / "progress.py").unlink()
        errors = validate_agent_skills(root)
        self.assertIn(
            "Referenced command module does not exist: tools.verification.progress",
            errors,
        )


    def change_manifest(self, root, change):
        path = root / ".agents/skills/mjobs-delivery/manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_non_object_manifest_returns_diagnostics(self):
        root = self.make_root()
        (root / ".agents/skills/mjobs-delivery/manifest.json").write_text("[]", encoding="utf-8")
        self.assertTrue(validate_agent_skills(root))

    def test_duplicate_playbook_is_rejected(self):
        root = self.make_root()
        self.change_manifest(root, lambda d: d["required_playbooks"].append(PLAYBOOKS[0]))
        self.assertTrue(validate_agent_skills(root))

    def test_non_string_playbook_returns_diagnostics(self):
        root = self.make_root()
        self.change_manifest(root, lambda d: d["required_playbooks"].append({"path": "x"}))
        self.assertTrue(validate_agent_skills(root))

    def test_unterminated_frontmatter_is_rejected(self):
        root = self.make_root()
        path = root / ".agents/skills/mjobs-delivery/SKILL.md"
        path.write_text(SKILL.replace("description: Test skill.\n---", "description: Test skill."), encoding="utf-8")
        self.assertTrue(validate_agent_skills(root))

    def test_manifest_identity_and_version_are_checked(self):
        for key, value in (("skill", "other"), ("schema_version", 7), ("schema_version", True)):
            with self.subTest(key=key, value=value):
                root = self.make_root()
                self.change_manifest(root, lambda d: d.update({key: value}))
                self.assertTrue(validate_agent_skills(root))

    def test_empty_required_lists_cannot_disable_checks(self):
        for key in ("required_paths", "command_modules"):
            with self.subTest(key=key):
                root = self.make_root()
                self.change_manifest(root, lambda d: d.update({key: []}))
                self.assertTrue(validate_agent_skills(root))

    def test_empty_playbook_is_rejected(self):
        root = self.make_root()
        (root / ".agents/skills/mjobs-delivery" / PLAYBOOKS[0]).write_text("", encoding="utf-8")
        self.assertTrue(validate_agent_skills(root))

    def test_unlisted_broken_python_command_is_rejected(self):
        root = self.make_root()
        path = root / ".agents/skills/mjobs-delivery" / PLAYBOOKS[0]
        path.write_text("Run `python -m tools.verification.does_not_exist`.", encoding="utf-8")
        self.assertTrue(validate_agent_skills(root))

    def test_path_outside_repository_is_rejected(self):
        root = self.make_root()
        self.change_manifest(root, lambda d: d["required_paths"].append("../outside"))
        self.assertTrue(validate_agent_skills(root))

    def test_non_utf8_skill_returns_diagnostics(self):
        root = self.make_root()
        (root / ".agents/skills/mjobs-delivery/SKILL.md").write_bytes(b"\xff\xfe\x80")
        self.assertTrue(validate_agent_skills(root))


if __name__ == "__main__":
    unittest.main()
