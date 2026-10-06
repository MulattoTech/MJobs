"""Validate the repository delivery skill without executing referenced commands."""
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
import re

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PLAYBOOKS = {
    "references/db-migration.md", "references/source-adapter.md",
    "references/application-adapter.md", "references/privacy-security.md",
    "references/llm-provider.md", "references/release-verification.md",
}
CORE_PATHS = {
    "AGENTS.md", "planning/IMPLEMENTATION_STATE.json", "planning/PR_QUEUE.json",
    "docs/evidence", "tools/verification/catalogue.py", "tools/verification/progress.py",
    "docs/reference/REFERENCE_PROJECTS.json",
    "docs/reference/REFERENCE_IMPLEMENTATION_POLICY.md", "THIRD_PARTY_NOTICES.md",
}
CORE_MODULES = {
    "tools.verification.agent_skills", "tools.verification.catalogue",
    "tools.verification.progress", "tools.verification.reference_provenance",
}
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _frontmatter(text: str) -> dict[str, str]:
    """Read this project's scalar header; missing/duplicate delimiters fail closed."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}
    result = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in result:
            return {}
        result[key] = value.strip().strip("\"'").strip()
    return result


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def _contained(base: Path, relative: str) -> Path | None:
    """Reject absolute/traversing paths on either host OS, including symlink escapes."""
    path = Path(relative)
    windows = PureWindowsPath(relative)
    if path.is_absolute() or windows.drive or windows.root or ".." in windows.parts:
        return None
    target = (base / path).resolve()
    return target if target.is_relative_to(base.resolve()) else None


def validate_agent_skills(root: Path) -> list[str]:
    root = root.resolve()
    skill_root = root / ".agents/skills/mjobs-delivery"
    errors: list[str] = []
    try:
        text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ["Missing or unreadable project skill: .agents/skills/mjobs-delivery/SKILL.md"]
    meta = _frontmatter(text)
    if meta.get("name") != "mjobs-delivery":
        errors.append("Project skill frontmatter name must be mjobs-delivery in a closed header")
    if not meta.get("description"):
        errors.append("Project skill needs a nonempty description")
    try:
        manifest = json.loads((skill_root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return errors + ["Project skill manifest is missing or invalid JSON"]
    if not isinstance(manifest, dict):
        return errors + ["Project skill manifest must be an object"]
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        errors.append("Project skill manifest schema_version must be 1")
    if manifest.get("skill") != "mjobs-delivery":
        errors.append("Project skill manifest identity must be mjobs-delivery")

    playbooks = manifest.get("required_playbooks")
    if (not _strings(playbooks) or len(playbooks) != len(EXPECTED_PLAYBOOKS)
            or set(playbooks) != EXPECTED_PLAYBOOKS):
        errors.append("Project skill manifest must list the six required playbooks exactly once")
    texts = [text]
    # Validate the required contract even when the manifest tries to omit a check.
    for relative in sorted(EXPECTED_PLAYBOOKS):
        target = _contained(skill_root, relative)
        try:
            if target is None:
                raise OSError("outside skill directory")
            content = target.read_text(encoding="utf-8")
            if not content.strip():
                errors.append(f"Empty project skill playbook: {relative}")
            texts.append(content)
        except (OSError, UnicodeError):
            errors.append(f"Missing project skill playbook: {relative}")
        if relative not in text:
            errors.append(f"SKILL.md does not route to playbook: {relative}")

    paths = manifest.get("required_paths")
    if not _strings(paths) or not CORE_PATHS.issubset(paths):
        errors.append("Project skill required_paths must contain the core repository paths")
    for relative in sorted(CORE_PATHS | set(paths if _strings(paths) else [])):
        target = _contained(root, relative)
        if target is None or not target.exists():
            errors.append(f"Referenced repository path does not exist: {relative}")

    modules = manifest.get("command_modules")
    if not _strings(modules) or not CORE_MODULES.issubset(modules):
        errors.append("Project skill command_modules must contain the core verification commands")
    referenced = set(re.findall(r"\bpython(?:3)?\s+-m\s+([A-Za-z_][A-Za-z0-9_.]*)", "\n".join(texts)))
    for module in sorted(CORE_MODULES | referenced | set(modules if _strings(modules) else [])):
        if not MODULE.fullmatch(module):
            errors.append(f"Invalid command module reference: {module!r}")
            continue
        if module == "unittest":  # Standard-library runner, not a repository module.
            continue
        path = _contained(root, module.replace(".", "/") + ".py")
        if path is None or not path.is_file():
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
