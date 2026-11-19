"""Plan and optionally execute conservative changed-file verification."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def collect_changed_paths(root: Path, base: str = "origin/main") -> list[str]:
    """Collect committed, staged, unstaged, and untracked paths conservatively."""
    root = root.resolve()
    paths: set[str] = set()
    diff_refs = (
        _git(root, "diff", "--name-only", f"{base}...HEAD"),
        _git(root, "diff", "--name-only"),
        _git(root, "diff", "--cached", "--name-only"),
        _git(root, "ls-files", "--others", "--exclude-standard"),
    )
    for output in diff_refs:
        for line in output.splitlines():
            path = line.strip().replace("\\", "/")
            if path:
                paths.add(path)
    return sorted(paths)


def _check(display: str, rationale: str, argv: list[str] | None = None) -> dict:
    return {
        "display": display,
        "argv": argv if argv is not None else shlex.split(display, posix=os.name != "nt"),
        "rationale": rationale,
    }


def _always_checks() -> list[dict]:
    return [
        _check("git diff --check", "Always-required whitespace and patch-integrity check."),
        _check("python -m tools.verification.catalogue", "Always-required requirement catalogue integrity."),
        _check("python -m tools.verification.progress validate", "Always-required progress and acceptance validation."),
        _check("python -m tools.verification.progress_dashboard --check", "Always-required progress dashboard consistency."),
        _check("python -m tools.verification.agent_skills", "Always-required repository skill and instruction integrity."),
        _check("python -m tools.ci.check_repository", "Always-required repository policy and workflow checks."),
    ]


def _is_broad_path(path: str) -> str | None:
    normalized = path.lower()
    if normalized.startswith(("planning/", ".github/workflows/", "docs/spec/", "alembic/", "migrations/")):
        if normalized.startswith("planning/"):
            return "protected planning authority"
        if normalized.startswith(".github/workflows/"):
            return "CI workflow contract"
        if normalized.startswith("docs/spec/"):
            return "shared specification contract"
        return "database migration"
    if normalized in {
        "requirements.txt",
        "requirements.lock",
        "package-lock.json",
        "pyproject.toml",
        "apps/web/package-lock.json",
        "apps/web/package.json",
    } or normalized.endswith(("package-lock.json", "poetry.lock", "uv.lock")):
        return "dependency or package contract"
    if normalized.startswith(("apps/api/", "services/", "db/", "models/")):
        return "shared API, service, persistence, or model contract"
    if normalized.endswith((".sql", ".proto", ".graphql")):
        return "shared data or API contract"
    return None


def _mapped_check_paths(root: Path, paths: list[str]) -> tuple[list[dict], list[str]]:
    checks: list[dict] = []
    reasons: list[str] = []
    python_targets: set[str] = set()
    frontend_targets: set[str] = set()
    broad = False

    for path in paths:
        normalized = path.replace("\\", "/")
        broad_reason = _is_broad_path(normalized)
        if broad_reason:
            broad = True
            reasons.append(f"{normalized}: {broad_reason}")
            continue

        if normalized == "tools/agent_context.py" or normalized.startswith("tools/agent_context"):
            python_targets.update({
                "tests/unit/test_agent_context.py",
                "tests/unit/test_agent_context_child_ids.py",
            })
        elif normalized.endswith(".py"):
            if normalized.startswith("tests/"):
                python_targets.add(normalized)
            elif normalized.startswith("tools/verification/"):
                stem = Path(normalized).stem
                candidate = root / "tests/unit" / f"test_{stem}.py"
                if candidate.is_file():
                    python_targets.add(candidate.relative_to(root).as_posix())
            elif normalized.startswith("tools/ci/"):
                stem = Path(normalized).stem
                candidate = root / "tests/unit" / f"test_{stem}.py"
                if candidate.is_file():
                    python_targets.add(candidate.relative_to(root).as_posix())

        if normalized.startswith("apps/web/src/"):
            if normalized.endswith((".test.ts", ".test.tsx")):
                frontend_targets.add(normalized.removeprefix("apps/web/"))
            else:
                suffix = "".join(Path(normalized).suffixes)
                stem = normalized[: -len(suffix)] if suffix else normalized
                for candidate_suffix in (".test.tsx", ".test.ts"):
                    candidate = root / f"{stem}{candidate_suffix}"
                    if candidate.is_file():
                        frontend_targets.add(candidate.relative_to(root / "apps/web").as_posix())

    if python_targets:
        targets = " ".join(sorted(python_targets))
        checks.append(_check(
            f"python -m pytest -q {targets}",
            "Focused Python tests selected for the explicitly modified Python/test paths.",
        ))

    if frontend_targets:
        targets = " ".join(sorted(frontend_targets))
        checks.append(_check(
            f"npm test --prefix apps/web -- {targets}",
            "Focused frontend Vitest files selected for modified source/test paths.",
        ))
        checks.append(_check(
            "npm run typecheck --prefix apps/web",
            "Frontend type contract check for source or test changes.",
        ))

    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")) and normalized.startswith("apps/web/"):
            if not any(check["display"].startswith("npm run typecheck") for check in checks):
                checks.append(_check(
                    "npm run typecheck --prefix apps/web",
                    f"Frontend source contract check for {normalized}.",
                ))
            if not any(check["display"].startswith("npm run lint") for check in checks):
                checks.append(_check(
                    "npm run lint --prefix apps/web",
                    f"Frontend lint check for {normalized}.",
                ))
            break

    if broad:
        checks.append(_check(
            "python -m pytest -q tests/unit",
            "Conservative broad Python suite: " + "; ".join(reasons) + ".",
        ))

    return checks, reasons


def build_verification_plan(root: Path, paths: list[str], base: str = "origin/main") -> dict:
    root = root.resolve()
    changed_paths = sorted({path.replace("\\", "/") for path in paths if path.strip()})
    if not changed_paths:
        changed_paths = collect_changed_paths(root, base)

    always = _always_checks()
    focused, broad_reasons = _mapped_check_paths(root, changed_paths)
    broad = bool(broad_reasons)
    unmapped = [
        path for path in changed_paths
        if not _is_broad_path(path)
        and not (
            path == "tools/agent_context.py"
            or path.startswith(("tools/agent_context", "tests/", "tools/verification/", "tools/ci/"))
            or (path.startswith("apps/web/src/") and path.endswith((".ts", ".tsx", ".js", ".jsx")))
            or path.startswith(("apps/web/package", "apps/web/scripts/"))
            or (path.startswith("docs/") and path.endswith(".md"))
            or (path.startswith(".github/") and path.endswith((".yml", ".yaml")))
        )
    ]

    if unmapped:
        broad = True
        broad_reasons.extend(f"{path}: unmapped changed path" for path in unmapped)
        for path in unmapped:
            always.append(_check(
                "python -m pytest -q tests/unit",
                f"Ambiguous/unmapped path {path} requires broader validation.",
            ))

    checks = always + focused
    if broad:
        if not any(check["display"] == "python -m pytest -q tests/unit" for check in checks):
            checks.append(_check(
                "python -m pytest -q tests/unit",
                "Broad validation: " + "; ".join(broad_reasons) + ".",
            ))

    profile = "broad" if broad else "focused"
    return {
        "base": base,
        "changed_paths": changed_paths,
        "profile": profile,
        "recommend_full_verification": broad,
        "broad_reasons": broad_reasons,
        "unmapped_paths": unmapped,
        "checks": checks,
        "dry_run": True,
    }


def execute_verification_plan(root: Path, plan: dict) -> dict:
    root = root.resolve()
    results = []
    for check in plan["checks"]:
        proc = subprocess.run(
            check["argv"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        results.append({
            "display": check["display"],
            "rationale": check["rationale"],
            "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        })
    return {
        "plan": plan,
        "dry_run": False,
        "results": results,
        "passed": all(result["returncode"] == 0 for result in results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--execute", action="store_true",
                        help="Execute the proposed checks; dry-run/preview is the default.")
    args = parser.parse_args(argv)
    plan = build_verification_plan(args.root.resolve(), args.path, args.base)
    if args.execute:
        result = execute_verification_plan(args.root.resolve(), plan)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
