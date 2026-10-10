"""Resolve the MJobs Python environment for isolated coding-agent worktrees.

Agent worktrees intentionally do not contain installed .venv packages. Rather
than falling back to a global Python that lacks repository requirements, use
a compatible existing checkout or primary MJobs .venv. Never install packages,
copy virtual environments, or mutate user repositories here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REQUIRED_PACKAGES = ("playwright", "sqlalchemy")


def _python_at(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _dependencies_match(root: Path, workspace: Path) -> bool:
    base = root / "requirements.lock"
    target = workspace / "requirements.lock"
    # Only share environments across worktrees containing compatible pinned
    # requirements. New deps must be installed intentionally at a checkpoint.
    if base.is_file() and target.is_file():
        return base.read_bytes() == target.read_bytes()
    return True


def resolve_python_environment(root: Path, workspace: Path) -> dict[str, Any]:
    """Inspect an existing MJobs venv, without downloads, installs or filesystem writes."""
    root = Path(root).resolve()
    workspace = Path(workspace).resolve()
    if not _dependencies_match(root, workspace):
        raise RuntimeError(
            "Worktree requirements.lock differs from main. Verify/install the "
            "new pinned dependencies in a suitable local venv before launching."
        )
    candidates = []
    for label, parent in (("worktree", workspace), ("MJobs primary checkout", root)):
        directory = parent / ".venv"
        interpreter = _python_at(directory)
        if interpreter.is_file() and directory not in [x[1] for x in candidates]:
            candidates.append((label, directory, interpreter))
    if not candidates:
        raise RuntimeError(
            "MJobs Python environment is missing. Create a Python venv and "
            "install requirements.lock; refuse to use global Python silently."
        )
    for label, directory, interpreter in candidates:
        try:
            probe = subprocess.run(
                [
                    str(interpreter), "-c",
                    "import json,sys,importlib.util;"
                    "print(json.dumps({'prefix':sys.prefix,'missing':["
                    "x for x in ('playwright','sqlalchemy')"
                    " if importlib.util.find_spec(x) is None]}))",
                ],
                cwd=str(workspace if workspace.is_dir() else root),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=12, creationflags=NO_WINDOW,
            )
            if probe.returncode != 0:
                continue
            result = json.loads(probe.stdout.strip())
            if not isinstance(result, dict):
                continue
            if result.get("missing"):
                continue
            # The selected python must actually be running in that environment.
            if Path(str(result.get("prefix", ""))).resolve() != directory.resolve():
                continue
            return {
                "venv": str(directory),
                "interpreter": str(interpreter),
                "source": label,
                "verified_modules": list(REQUIRED_PACKAGES),
            }
        except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    raise RuntimeError(
        "The MJobs venv exists but required packages Playwright and SQLAlchemy "
        "are unavailable. Verify the pinned environment; do not run tests "
        "using global Python as a fallback."
    )


def build_subprocess_environment(
    base: dict[str, str], *, venv_dir: str | None = None,
) -> dict[str, str]:
    """Prepend the selected Python environment only for the owned CLI subprocess."""
    env = dict(base)
    if venv_dir is None:
        return env
    directory = Path(venv_dir)
    interpreter = _python_at(directory)
    if not interpreter.is_file():
        raise RuntimeError(
            "Approved MJobs Python environment disappeared before CLI start; "
            "stop instead of silently falling back to global Python."
        )
    bins = interpreter.parent
    original = env.get("PATH", env.get("Path", ""))
    # Windows environment variables are case-insensitive, but Python mappings
    # aren't. Remove alternate spellings before passing to CreateProcess.
    for key in list(env):
        if key.casefold() == "path" and key != "PATH":
            env.pop(key, None)
    env["PATH"] = str(bins) + os.pathsep + original
    env["VIRTUAL_ENV"] = str(directory)
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_REQUIRE_VIRTUALENV"] = "1"
    env["MJOBS_PYTHON_INTERPRETER"] = str(interpreter)
    env.pop("PYTHONHOME", None)
    return env
