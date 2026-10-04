"""Validate project-scoped Codex/agent skill contracts."""
from __future__ import annotations

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "mjobs-delivery"
SKILL_PATH = SKILL_ROOT / "SKILL.md"
MANIFEST_PATH = SKILL_ROOT / "manifest.json"


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    try:
        block = text.split("---\n", 2)[1]
    except IndexError:
        return {}
    values: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def _module_path(root: Path, module: str) -> Path:
    return root / (module.replace(".", "/") + ".py")


def validate_agent_skills(root: Path) -> list[str]:
    root = root.resolve()
    skill_root = root / ".agents" / "skills" / "mjobs-delivery"
    skill_path = skill_root / "SKILL.md"
    manifest_path = skill_root / "manifest.json"
    errors: list[str] = []

    if not skill_path.is_file():
        return ["Missing project skill: .agents/skills/mjobs-delivery/SKILL.md"]
    text = skill_path.read_text(encoding="utf-8")
    meta = _frontmatter(text)
    if meta.get("name") != "mjobs-delivery":
        errors.append("Project skill frontmatter name must be mjobs-delivery")
    if not meta.get("description"):
        errors.append("Project skill needs a nonempty description")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return errors + ["Project skill manifest is missing or invalid JSON"]

    playbooks = manifest.get("required_playbooks")
    expected = {
        "references/db-migration.md",
        "references/source-adapter.md",
        "references/application-adapter.md",
        "references/privacy-security.md",
        "references/llm-provider.md",
        "references/release-verification.md",
    }
    if not isinstance(playbooks, list) or set(playbooks) != expected:
        errors.append("Project skill manifest must list the six required playbooks exactly once")
        playbooks = []
    for relative in playbooks:
        target = (skill_root / relative).resolve()
        if not target.is_relative_to(skill_root.resolve()) or not target.is_file():
            errors.append(f"Missing project skill playbook: {relative}")
            continue
        if relative not in text:
            errors.append(f"SKILL.md does not route to playbook: {relative}")

    required_paths = manifest.get("required_paths")
    if not isinstance(required_paths, list):
        errors.append("Project skill manifest required_paths must be a list")
        required_paths = []
    for relative in required_paths:
        if not isinstance(relative, str) or not relative.strip():
            errors.append("Project skill required_paths entries must be nonempty strings")
            continue
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.exists():
            errors.append(f"Referenced repository path does not exist: {relative}")

    modules = manifest.get("command_modules")
    if not isinstance(modules, list):
        errors.append("Project skill manifest command_modules must be a list")
        modules = []
    for module in modules:
        if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
            errors.append(f"Invalid command module reference: {module!r}")
            continue
        if not _module_path(root, module).is_file():
            errors.append(f"Referenced command module does not exist: {module}")

    return errors


def main() -> int:
    errors = validate_agent_skills(ROOT)
    for error in errors:
        print(error)
    print(f"Agent skill validation: {'FAIL' if errors else 'PASS'}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
