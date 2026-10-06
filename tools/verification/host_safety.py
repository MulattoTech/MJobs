"""Reject workstation-wide mutations in the checked-in MJobs setup scripts."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys


_SCRIPT_SUFFIXES = {".ps1", ".psm1", ".sh", ".bash", ".cmd", ".bat"}
_EXCLUDED_PARTS = {
    ".git", ".venv", ".venv-test", "node_modules", ".worktrees", "dist",
    "outputs", "__pycache__",
}
_RULES = (
    ("HOST_WIDE_PACKAGE_UPGRADE", re.compile(
        r"\bwinget\s+upgrade\s+--all\b|\bchoco(?:latey)?\s+upgrade\s+all\b|"
        r"\bapt(?:-get)?\s+(?:dist-)?upgrade\b|\bbrew\s+upgrade(?:\s|$)|"
        r"\b(?:dnf|yum)\s+(?:update|upgrade)(?:\s|$)|\bconda\s+update\s+--all\b",
        re.IGNORECASE,
    )),
    ("HOST_DRIVER_OR_FIRMWARE_CHANGE", re.compile(
        r"\bpnputil(?:\.exe)?\b.*\s/(?:add-driver|delete-driver)\b|"
        r"\bdevcon(?:\.exe)?\b.*\b(?:install|remove|update|disable|enable)\b|"
        r"\b(?:update|flash|modify|reset)\s+(?:the\s+)?(?:bios|uefi|firmware)\b|"
        r"\b(?:bios|uefi|firmware)\s+(?:update|flash|modify|reset)\b|"
        r"\b(?:update-firmware|set-biossetting|install-windowsdriver)\b|"
        r"\bdism(?:\.exe)?\b.*\b/add-driver\b|"
        r"\b(?:nvidia|amd|intel)\s+(?:graphics\s+)?driver\s+(?:install|upgrade|downgrade|reinstall)\b|"
        r"\b(?:winget|choco(?:latey)?)\s+upgrade\b.*\b(?:driver|firmware)\b",
        re.IGNORECASE,
    )),
    ("HOST_NETWORK_RECONFIGURATION", re.compile(
        r"\bnetsh\b.*\b(?:winsock\s+reset|int(?:erface)?\s+ip\s+reset|interface\s+set\s+interface)\b|"
        r"\b(?:disable|enable|restart|remove)-netadapter\b|"
        r"\bset-netadapter\b|\bipconfig\s+/(?:release|renew)\b|\broute(?:\.exe)?\s+-f\b",
        re.IGNORECASE,
    )),
    ("HOST_REBOOT", re.compile(
        r"\bshutdown(?:\.exe)?\b.*(?:/[rsg]\b|--reboot\b|--poweroff\b)|"
        r"\b(?:restart-computer|stop-computer|reboot|poweroff|halt)\b|"
        r"\bsystemctl\s+(?:reboot|poweroff|halt)\b|\binit\s+[06]\b",
        re.IGNORECASE,
    )),
    ("BROAD_PROCESS_TERMINATION", re.compile(
        r"\btaskkill(?:\.exe)?\b.*(?:/im\b|/fi\b)|\bstop-process\b.*(?:-name\b|-id\s+\*|--name\b)|"
        r"\b(?:killall|pkill)\b",
        re.IGNORECASE,
    )),
    ("HOST_GRAPHICS_TDR_CHANGE", re.compile(
        r"\b(?:tdrdelay|tdrdiac|tdrlevel|tdrdebugmode)\b|"
        r"\b(?:set-itemproperty|new-itemproperty|reg(?:\.exe)?\s+add)\b.*\bgraphicsdrivers\b",
        re.IGNORECASE,
    )),
)


def _check_text(text: str, relative: str) -> list[str]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        command = line.split("#", 1)[0]
        for code, pattern in _RULES:
            if pattern.search(command):
                findings.append(f"{code}:{relative}:{number}")
    return findings


def validate_host_safety(root: Path) -> list[str]:
    """Report workstation-wide destructive commands in launch/setup scripts."""
    root = Path(root).resolve()
    if not root.is_dir():
        return ["REPOSITORY_ROOT_MISSING"]
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _EXCLUDED_PARTS.intersection(path.relative_to(root).parts):
            continue
        suffix = path.suffix.casefold()
        if suffix in _SCRIPT_SUFFIXES:
            relative = path.relative_to(root).as_posix()
            try:
                findings.extend(_check_text(path.read_text(encoding="utf-8"), relative))
            except (OSError, UnicodeDecodeError):
                findings.append(f"SCRIPT_UNREADABLE:{relative}")
        elif path.name.casefold() == "package.json":
            relative = path.relative_to(root).as_posix()
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                findings.append(f"PACKAGE_MANIFEST_INVALID:{relative}")
                continue
            scripts = manifest.get("scripts", {})
            if isinstance(scripts, dict):
                for name, command in sorted(scripts.items()):
                    if isinstance(name, str) and isinstance(command, str):
                        for code, pattern in _RULES:
                            if pattern.search(command):
                                findings.append(f"{code}:{relative}:scripts.{name}")
    return sorted(set(findings))


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    findings = validate_host_safety(root)
    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
        return 1
    print("PASS - project setup scripts do not perform prohibited workstation-wide changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
