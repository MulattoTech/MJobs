"""Stage, validate, install and roll back AFK-DEV skill candidates."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def install_candidate(candidate, installed, history, *, verify, post_verify=lambda path: True):
    candidate, installed, history = Path(candidate).resolve(), Path(installed).resolve(), Path(history).resolve()
    if candidate == installed or not (candidate / "SKILL.md").is_file():
        raise ValueError("Candidate must be a separate complete skill directory")
    if not verify(candidate):
        return False
    installed.parent.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)
    staged = installed.parent / (".afk-dev-candidate-" + uuid.uuid4().hex)
    backup = history / ("afk-dev-" + uuid.uuid4().hex)
    shutil.copytree(candidate, staged)
    if not verify(staged):
        shutil.rmtree(staged)
        return False
    had_installed = installed.exists()
    try:
        if had_installed:
            os.replace(installed, backup)
        os.replace(staged, installed)
        if not post_verify(installed):
            raise RuntimeError("Post-install smoke failed")
        return True
    except Exception:
        if installed.exists():
            failed = history / ("failed-" + uuid.uuid4().hex)
            os.replace(installed, failed)
        if had_installed and backup.exists():
            os.replace(backup, installed)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def run_validation(candidate, behavioral_command):
    validator = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py"
    checks = [
        [sys.executable, str(validator), str(candidate)],
        [sys.executable, "-m", "unittest", "discover", "-s", str(candidate / "tests"), "-p", "test_*.py", "-q"],
        behavioral_command,
    ]
    return all(subprocess.run(command, cwd=candidate, check=False).returncode == 0 for command in checks)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--installed", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--behavior-command", required=True, help="JSON argv array for fresh behavioral regression gate")
    args = parser.parse_args(argv)
    import json
    behavior = json.loads(args.behavior_command)
    if not isinstance(behavior, list) or not behavior:
        parser.error("--behavior-command must be nonempty JSON argv array")
    ok = install_candidate(args.candidate, args.installed, args.history, verify=lambda path: run_validation(path, behavior))
    print("installed" if ok else "candidate rejected")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
