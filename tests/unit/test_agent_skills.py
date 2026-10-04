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
            ],
            "command_modules": [
                "tools.verification.agent_skills",
                "tools.verification.catalogue",
                "tools.verification.progress",
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


if __name__ == "__main__":
    unittest.main()
