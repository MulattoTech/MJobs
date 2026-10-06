"""Persist and validate evidence-linked implementation checkpoints."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "planning/IMPLEMENTATION_STATE.json"
LIST_FIELDS = (
    "completed_steps", "files_added", "files_modified", "tests_completed",
    "tests_remaining", "migrations_applied", "live_integrations_tested",
    "known_errors", "known_limitations",
)


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("UTC required")
    return stamp


def _entry_errors(root: Path, entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return ["Milestone must be an object"]
    errors = []
    for key in ("milestone_id", "current_phase"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            errors.append(f"Missing milestone {key}")
    try:
        _utc_timestamp(entry["timestamp"])
    except (KeyError, AttributeError, TypeError, ValueError):
        errors.append("Milestone timestamp must be UTC ISO-8601")
    for key in LIST_FIELDS:
        value = entry.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip()
                                              for item in value):
            errors.append(f"Milestone {key} must be a list of nonempty strings")
    checkpoint = entry.get("last_safe_checkpoint")
    if not isinstance(checkpoint, dict):
        errors.append("Milestone needs last_safe_checkpoint")
    else:
        commit = checkpoint.get("git_commit")
        evidence = checkpoint.get("evidence_path")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            errors.append("Checkpoint needs a full Git commit SHA")
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append("Checkpoint needs an evidence path")
        else:
            root = root.resolve()
            target = (root / evidence).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                errors.append(f"Checkpoint evidence is missing or outside repository: {evidence}")
    return errors


def validate_journal(root: Path) -> list[str]:
    state = json.loads((root / "planning/IMPLEMENTATION_STATE.json").read_text(encoding="utf-8"))
    entries = state.get("milestone_journal")
    if not isinstance(entries, list) or not entries:
        return ["Implementation milestone journal is empty or missing"]
    errors = []
    seen = set()
    previous = None
    for entry in entries:
        errors.extend(_entry_errors(root, entry))
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("milestone_id")
        if identifier in seen:
            errors.append(f"Duplicate milestone: {identifier}")
        seen.add(identifier)
        timestamp = entry.get("timestamp")
        try:
            stamp = _utc_timestamp(timestamp)
            if previous is not None and stamp < previous:
                errors.append(f"Milestones out of timestamp order: {identifier}")
            previous = stamp
        except ValueError:
            pass
    if isinstance(entries[-1], dict):
        if entries[-1].get("current_phase") != state.get("current_phase"):
            errors.append("Current phase disagrees with last milestone")
        if entries[-1].get("last_safe_checkpoint") != state.get("last_safe_checkpoint"):
            errors.append("Last safe checkpoint disagrees with last milestone")
    return errors


def resume_snapshot(root: Path = ROOT) -> dict:
    """Return a validated, read-only summary for a fresh implementation session."""
    root = root.resolve()
    state = json.loads((root / "planning/IMPLEMENTATION_STATE.json").read_text(encoding="utf-8"))
    errors = validate_journal(root)
    if errors:
        raise ValueError("Implementation journal cannot resume: " + "; ".join(errors))
    entries = state["milestone_journal"]
    latest = entries[-1]
    return {
        "schema_version": state.get("schema_version"),
        "current_milestone": state.get("current_milestone"),
        "current_objective": state.get("current_objective"),
        "status": state.get("status"),
        "active_development_issue": state.get("active_development_issue"),
        "current_phase": latest["current_phase"],
        "completed_steps": latest["completed_steps"],
        "files_added": latest["files_added"],
        "files_modified": latest["files_modified"],
        "tests_completed": latest["tests_completed"],
        "tests_remaining": latest["tests_remaining"],
        "migrations_applied": latest["migrations_applied"],
        "live_integrations_tested": latest["live_integrations_tested"],
        "known_errors": latest["known_errors"],
        "known_limitations": latest["known_limitations"],
        "last_safe_checkpoint": latest["last_safe_checkpoint"],
        "latest_milestone": latest,
    }


def append_milestone(state_path: Path, entry: dict) -> None:
    """Atomically replace one state file after validating the new checkpoint."""
    root = state_path.parent.parent
    errors = _entry_errors(root, entry)
    if errors:
        raise ValueError("; ".join(errors))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    entries = state.setdefault("milestone_journal", [])
    if not isinstance(entries, list):
        raise ValueError("Implementation milestone journal is not a list")
    if entries:
        prior_errors = validate_journal(root)
        if prior_errors:
            raise ValueError("Existing milestone journal is invalid: " + "; ".join(prior_errors))
    if any(item.get("milestone_id") == entry["milestone_id"] for item in entries):
        raise ValueError(f"Duplicate milestone: {entry['milestone_id']}")
    if entries and _utc_timestamp(entry["timestamp"]) < _utc_timestamp(entries[-1]["timestamp"]):
        raise ValueError("Milestone timestamp precedes existing checkpoint")
    entries.append(entry)
    state["current_phase"] = entry["current_phase"]
    state["last_safe_checkpoint"] = entry["last_safe_checkpoint"]
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=state_path.parent,
        prefix=".implementation-state-", suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, state_path)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    record = command.add_parser("record", help="Append one verified milestone to implementation state")
    record.add_argument("--entry", required=True, type=Path, help="JSON milestone with evidence path")
    command.add_parser("validate", help="Validate the durable milestone journal")
    command.add_parser("resume", help="Print a validated read-only checkpoint for a fresh session")
    args = parser.parse_args()
    if args.command == "record":
        append_milestone(STATE_PATH, json.loads(args.entry.read_text(encoding="utf-8")))
    elif args.command == "resume":
        print(json.dumps(resume_snapshot(ROOT), indent=2, ensure_ascii=False))
        return 0
    errors = validate_journal(ROOT)
    for error in errors:
        print(error)
    print(f"Progress journal validation: {'FAIL' if errors else 'PASS'}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
