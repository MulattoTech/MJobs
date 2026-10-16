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
DEFAULT_OUTPUT_JOURNAL = "outputs/job-platform-implementation-state.json"
OUTPUT_STATE_PATH = ROOT / DEFAULT_OUTPUT_JOURNAL
LIST_FIELDS = (
    "completed_steps", "files_added", "files_modified", "tests_completed",
    "tests_remaining", "migrations_applied", "live_integrations_tested",
    "known_errors", "known_limitations",
)
AUTHORITATIVE_FIELDS = (
    "current_phase",
    "completed_steps",
    "files_added",
    "files_modified",
    "tests_completed",
    "tests_remaining",
    "migrations_applied",
    "live_integrations_tested",
    "known_errors",
    "known_limitations",
    "last_safe_checkpoint",
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


def extract_machine_readable_state(entry: dict, state: dict | None = None) -> dict:
    """Extract authoritative 11-field machine-readable implementation checkpoint."""
    result = {
        "current_phase": entry["current_phase"],
        "completed_steps": list(entry["completed_steps"]),
        "files_added": list(entry["files_added"]),
        "files_modified": list(entry["files_modified"]),
        "tests_completed": list(entry["tests_completed"]),
        "tests_remaining": list(entry["tests_remaining"]),
        "migrations_applied": list(entry["migrations_applied"]),
        "live_integrations_tested": list(entry["live_integrations_tested"]),
        "known_errors": list(entry["known_errors"]),
        "known_limitations": list(entry["known_limitations"]),
        "last_safe_checkpoint": dict(entry["last_safe_checkpoint"]),
    }
    if "milestone_id" in entry:
        result["milestone_id"] = entry["milestone_id"]
    if "timestamp" in entry:
        result["timestamp"] = entry["timestamp"]
    if state and "schema_version" in state:
        result["schema_version"] = state["schema_version"]
    return result


def write_atomic_json(target_path: Path, data: dict) -> None:
    """Atomically write JSON with directory creation, fsync, and safe replacement."""
    target_path = target_path.resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target_path.parent,
        prefix=f".{target_path.stem}-", suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target_path)
    finally:
        temp_path.unlink(missing_ok=True)


def validate_outputs_journal(root: Path, outputs_path: Path | None = None) -> list[str]:
    """Validate authoritative 11-field outputs journal."""
    path = (outputs_path or (root / DEFAULT_OUTPUT_JOURNAL)).resolve()
    if not path.is_file():
        return [f"Outputs journal does not exist: {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"Outputs journal is invalid JSON: {exc}"]
    if not isinstance(data, dict):
        return ["Outputs journal must be a JSON object"]
    errors = []
    if not isinstance(data.get("current_phase"), str) or not data["current_phase"].strip():
        errors.append("Outputs journal missing valid current_phase")
    for key in LIST_FIELDS:
        value = data.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip()
                                              for item in value):
            errors.append(f"Outputs journal {key} must be a list of nonempty strings")
    checkpoint = data.get("last_safe_checkpoint")
    if not isinstance(checkpoint, dict):
        errors.append("Outputs journal needs last_safe_checkpoint")
    else:
        commit = checkpoint.get("git_commit")
        evidence = checkpoint.get("evidence_path")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            errors.append("Outputs journal checkpoint needs a full Git commit SHA")
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append("Outputs journal checkpoint needs an evidence path")
        else:
            root = root.resolve()
            target = (root / evidence).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                errors.append(f"Outputs journal evidence is missing or outside repository: {evidence}")
    return errors


def validate_journal(root: Path, journal_path: Path | None = None) -> list[str]:
    root = root.resolve()
    target_state = (journal_path or (root / "planning/IMPLEMENTATION_STATE.json")).resolve()
    if not target_state.is_file():
        return [f"Implementation state file missing: {target_state}"]
    state = json.loads(target_state.read_text(encoding="utf-8"))
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

    outputs_path = root / DEFAULT_OUTPUT_JOURNAL
    if outputs_path.is_file() and not errors:
        out_errors = validate_outputs_journal(root, outputs_path)
        errors.extend(out_errors)
        if not out_errors:
            out_data = json.loads(outputs_path.read_text(encoding="utf-8"))
            if out_data.get("current_phase") != entries[-1].get("current_phase"):
                errors.append("Outputs journal current_phase disagrees with latest milestone")
            if out_data.get("last_safe_checkpoint") != entries[-1].get("last_safe_checkpoint"):
                errors.append("Outputs journal last_safe_checkpoint disagrees with latest milestone")
    return errors


def sync_implementation_state(root: Path = ROOT, output_path: Path | None = None) -> Path:
    """Generate or refresh outputs/job-platform-implementation-state.json from the current state."""
    root = root.resolve()
    state_file = root / "planning/IMPLEMENTATION_STATE.json"
    if not state_file.is_file():
        raise FileNotFoundError(f"State file not found: {state_file}")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    entries = state.get("milestone_journal")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Implementation milestone journal is empty or missing")
    errors = validate_journal(root)
    if errors:
        raise ValueError("Cannot sync from invalid journal: " + "; ".join(errors))
    latest = entries[-1]
    machine_state = extract_machine_readable_state(latest, state)
    target = (output_path or (root / DEFAULT_OUTPUT_JOURNAL)).resolve()
    write_atomic_json(target, machine_state)
    return target


def _resume_from_outputs_journal(root: Path, payload: dict) -> dict:
    errors = []
    if not isinstance(payload.get("current_phase"), str) or not payload["current_phase"].strip():
        errors.append("Outputs journal missing valid current_phase")
    for key in LIST_FIELDS:
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip()
                                              for item in value):
            errors.append(f"Outputs journal {key} must be a list of nonempty strings")
    checkpoint = payload.get("last_safe_checkpoint")
    if not isinstance(checkpoint, dict):
        errors.append("Outputs journal needs last_safe_checkpoint")
    else:
        commit = checkpoint.get("git_commit")
        evidence = checkpoint.get("evidence_path")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            errors.append("Outputs journal checkpoint needs a full Git commit SHA")
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append("Outputs journal checkpoint needs an evidence path")
        else:
            target = (root / evidence).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                errors.append(f"Checkpoint evidence is missing or outside repository: {evidence}")
    if errors:
        raise ValueError("Outputs implementation journal cannot resume: " + "; ".join(errors))
    return {
        "schema_version": payload.get("schema_version", 1),
        "current_milestone": payload.get("milestone_id", "RESUMED_FROM_OUTPUTS_JOURNAL"),
        "current_objective": payload.get("current_objective", "Resumed from outputs journal"),
        "status": payload.get("status", "PARTIAL"),
        "active_development_issue": payload.get("active_development_issue"),
        "current_phase": payload["current_phase"],
        "completed_steps": payload["completed_steps"],
        "files_added": payload["files_added"],
        "files_modified": payload["files_modified"],
        "tests_completed": payload["tests_completed"],
        "tests_remaining": payload["tests_remaining"],
        "migrations_applied": payload["migrations_applied"],
        "live_integrations_tested": payload["live_integrations_tested"],
        "known_errors": payload["known_errors"],
        "known_limitations": payload["known_limitations"],
        "last_safe_checkpoint": payload["last_safe_checkpoint"],
        "latest_milestone": payload,
    }


def resume_snapshot(root: Path = ROOT, source_path: Path | None = None) -> dict:
    """Return a validated, read-only summary for a fresh implementation session."""
    root = root.resolve()
    if source_path is not None:
        source = source_path.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Source state file not found: {source}")
        payload = json.loads(source.read_text(encoding="utf-8"))
        if "milestone_journal" in payload:
            errors = validate_journal(root, journal_path=source)
            if errors:
                raise ValueError("Implementation journal cannot resume: " + "; ".join(errors))
            latest = payload["milestone_journal"][-1]
            return {
                "schema_version": payload.get("schema_version"),
                "current_milestone": payload.get("current_milestone"),
                "current_objective": payload.get("current_objective"),
                "status": payload.get("status"),
                "active_development_issue": payload.get("active_development_issue"),
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
        return _resume_from_outputs_journal(root, payload)

    state_file = root / "planning/IMPLEMENTATION_STATE.json"
    outputs_file = root / DEFAULT_OUTPUT_JOURNAL

    if state_file.is_file():
        state = json.loads(state_file.read_text(encoding="utf-8"))
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
    elif outputs_file.is_file():
        payload = json.loads(outputs_file.read_text(encoding="utf-8"))
        return _resume_from_outputs_journal(root, payload)
    else:
        raise FileNotFoundError(f"Neither implementation state nor output journal found in {root}")


def append_milestone(state_path: Path, entry: dict, *, output_state_path: Path | None = None) -> None:
    """Atomically replace state and output journal after validating the new checkpoint."""
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

    write_atomic_json(state_path, state)
    out_target = (output_state_path or (root / DEFAULT_OUTPUT_JOURNAL)).resolve()
    machine_state = extract_machine_readable_state(entry, state)
    write_atomic_json(out_target, machine_state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    record = command.add_parser("record", help="Append one verified milestone to implementation state")
    record.add_argument("--entry", required=True, type=Path, help="JSON milestone with evidence path")
    record.add_argument("--output-path", type=Path, default=None, help="Optional output journal destination")

    resume_cmd = command.add_parser("resume", help="Print a validated read-only checkpoint for a fresh session")
    resume_cmd.add_argument("--source", type=Path, default=None, help="Source state file or outputs journal")

    sync_cmd = command.add_parser("sync", help="Synchronize outputs/job-platform-implementation-state.json from state")
    sync_cmd.add_argument("--output", type=Path, default=None, help="Destination outputs journal path")

    validate_cmd = command.add_parser("validate", help="Validate the durable milestone journal")
    validate_cmd.add_argument("--state", type=Path, default=None, help="Specific state file to validate")

    args = parser.parse_args()
    if args.command == "record":
        append_milestone(STATE_PATH, json.loads(args.entry.read_text(encoding="utf-8")),
                         output_state_path=args.output_path)
    elif args.command == "sync":
        synced = sync_implementation_state(ROOT, output_path=args.output)
        print(f"Synchronized implementation state to {synced}")
        return 0
    elif args.command == "resume":
        print(json.dumps(resume_snapshot(ROOT, source_path=args.source), indent=2, ensure_ascii=False))
        return 0
    errors = validate_journal(ROOT, journal_path=args.state)
    for error in errors:
        print(error)
    print(f"Progress journal validation: {'FAIL' if errors else 'PASS'}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
