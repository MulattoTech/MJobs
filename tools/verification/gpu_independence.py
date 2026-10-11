"""Validate that required MJobs dependencies and runtime configuration stay CPU-only."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from tools.ci.python_requirements import requirement_files


_REQUIRED_FILES = (
    "requirements.lock",
    "apps/web/package.json",
    "apps/web/package-lock.json",
    "compose.yml",
    "infra/docker/Dockerfile",
)
_GPU_DEPENDENCY = re.compile(
    r"^(?:cuda(?:[-_.].*)?|nvidia(?:[-_.].*)?|rocm(?:[-_.].*)?|torch(?:[-_.].*)?|"
    r"onnxruntime-gpu|paddlepaddle-gpu|tensorflow-gpu|tensorflow-rocm|cupy(?:[-_.].*)?|triton)$",
    re.IGNORECASE,
)
_PACKAGE_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
_GPU_RUNTIME_CONFIG = re.compile(
    r"^\s*(?:device_requests|devices)\s*:|"
    r"^\s*runtime\s*:\s*nvidia\b|"
    r"capabilities\s*:\s*\[[^\]]*\bgpu\b",
    re.IGNORECASE | re.MULTILINE,
)
_GPU_DOCKER_RUNTIME = re.compile(r"(?i)nvidia/cuda|\b(?:cuda|rocm)\b|--gpus(?:=|\s)")
_GPU_FRONTEND_API = re.compile(
    r"(?i)\bnavigator\s*\.\s*gpu\b|\bwebgl(?:renderingcontext)?\b|\bwebgpu\b"
)


def _package_findings(names: list[str], manifest: str) -> list[str]:
    return [f"GPU_DEPENDENCY:{manifest}:{name}" for name in sorted(set(names))
            if _GPU_DEPENDENCY.fullmatch(name)]


def _python_package_names(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.partition("#")[0].strip()
        match = _PACKAGE_NAME.match(line)
        if match and ("==" in line or ">=" in line or "<=" in line or "~=" in line):
            names.append(match.group(1).casefold().replace("_", "-"))
    return names


def _node_package_names(package_json: dict, package_lock: dict) -> list[str]:
    names = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        dependencies = package_json.get(section, {})
        if isinstance(dependencies, dict):
            names.extend(name for name in dependencies if isinstance(name, str))
    packages = package_lock.get("packages", {})
    if isinstance(packages, dict):
        for path in packages:
            if isinstance(path, str) and path.startswith("node_modules/"):
                names.append(path.removeprefix("node_modules/"))
    return [name.casefold().replace("_", "-") for name in names]


def validate_gpu_independence(root: Path) -> list[str]:
    """Return stable error codes when required MJobs runtime inputs require GPU support."""
    root = Path(root)
    findings = []
    missing = [relative for relative in _REQUIRED_FILES if not (root / relative).is_file()]
    findings.extend(f"REQUIRED_FILE_MISSING:{relative}" for relative in missing)
    if missing:
        return findings

    try:
        manifests = requirement_files(root)
        for manifest, content in manifests.items():
            findings.extend(_package_findings(_python_package_names(content), manifest))
    except (OSError, ValueError):
        findings.append("PYTHON_REQUIREMENTS_INVALID")

    try:
        package_json = json.loads((root / "apps/web/package.json").read_text(encoding="utf-8"))
        package_lock = json.loads((root / "apps/web/package-lock.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        findings.append("WEB_DEPENDENCY_MANIFEST_INVALID")
    else:
        findings.extend(_package_findings(
            _node_package_names(package_json, package_lock),
            "apps/web/package-lock.json",
        ))

    compose = (root / "compose.yml").read_text(encoding="utf-8")
    if _GPU_RUNTIME_CONFIG.search(compose):
        findings.append("GPU_RUNTIME_CONFIG:compose.yml")

    dockerfile = (root / "infra/docker/Dockerfile").read_text(encoding="utf-8")
    if _GPU_DOCKER_RUNTIME.search(dockerfile):
        findings.append("GPU_RUNTIME_CONFIG:infra/docker/Dockerfile")

    source_root = root / "apps/web/src"
    if source_root.is_dir():
        for source in sorted(source_root.rglob("*")):
            if source.is_file() and source.suffix.casefold() in {".js", ".jsx", ".ts", ".tsx"}:
                relative = source.relative_to(root).as_posix()
                if _GPU_FRONTEND_API.search(source.read_text(encoding="utf-8")):
                    findings.append(f"GPU_API_USAGE:{relative}")

    return sorted(set(findings))


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    findings = validate_gpu_independence(root)
    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
        return 1
    print("PASS - required dependencies, runtime configuration, and frontend APIs do not require GPU support")
    return 0


if __name__ == "__main__":
    sys.exit(main())
