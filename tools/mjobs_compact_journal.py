"""Compact historical MJobs journal after saving a verified external backup.

The Git history and external JSON backup retain the full journal.
No acceptance result is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "planning/IMPLEMENTATION_STATE.json"

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root", type=Path, required=True)
    args = parser.parse_args()
    src = STATE.read_bytes()
    state = json.loads(src)
    journal = state.get("milestone_journal")
    if not isinstance(journal, list) or not journal:
        raise ValueError("Expected a non-empty historical milestone journal")
    latest = journal[-1]
    if (latest.get("current_phase") != state.get("current_phase") or
        latest.get("last_safe_checkpoint") != state.get("last_safe_checkpoint")):
        raise ValueError("State checkpoint mismatch; refusing to compact")
    print(f"Milestones before: {len(journal)}; current state bytes: {len(src):,}")
    if len(journal) == 1:
        print("Already compacted; no change.")
        return
    if not args.apply:
        return
    args.backup_root.mkdir(parents=True, exist_ok=True)
    original = args.backup_root / "implementation-state-original-20261010.json"
    if original.exists():
        if original.read_bytes() != src:
            raise RuntimeError("Backup filename exists with different contents; refusing overwrite")
    else:
        original.write_bytes(src)
    digest = hashlib.sha256(src).hexdigest()
    if hashlib.sha256(original.read_bytes()).hexdigest() != digest:
        raise RuntimeError("Backup integrity verification failed")
    state["milestone_journal"] = [latest]
    state["journal_compaction"] = {
        "historic_entries_preserved_in_git_history": True,
        "archived_source_sha256": digest,
        "archived_entry_count": len(journal) - 1,
        "current_entry_preserved": latest.get("milestone_id"),
    }
    new_data = (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_bytes(new_data)
    try:
        tmp.replace(STATE)
    except PermissionError:
        # Some Windows endpoint-security tools block replacing open JSON
        # files, even when writing their contents is permitted.
        shutil.copyfile(tmp, STATE)
        tmp.unlink()
    print(f"Milestones after: 1; compact bytes: {len(new_data):,}; archived sha256: {digest}")

if __name__ == "__main__":
    main()
