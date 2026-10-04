"""Find exact non-secret IDs in Codex JSONL sessions without printing content."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")
SAFE_TOOL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
RECORD_TYPES = {"message", "custom_tool_call", "function_call", "custom_tool_call_output", "function_call_output", "item_completed"}
ROLES = {"user", "assistant", "system", "tool"}


def scan_sessions(root: Path, identifier: str, *, limit: int = 100) -> dict:
    if not ID_PATTERN.fullmatch(identifier) or not any(mark in identifier for mark in "-_:") or identifier.lower() in {"password", "secret", "credential"}:
        raise ValueError("Use an exact non-secret RunId or evidence ID, not arbitrary text")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Session root must be an existing directory")

    needle = identifier.encode("ascii")
    records = []
    files = 0
    matches = 0
    malformed_matches = 0
    for path in sorted(root.rglob("*.jsonl")):
        files += 1
        with path.open("rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                if needle not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, ValueError):
                    malformed_matches += 1
                    continue
                matches += 1
                if len(records) >= limit:
                    continue
                payload = row.get("payload") if isinstance(row, dict) else None
                payload = payload if isinstance(payload, dict) else {}
                record_type = payload.get("type")
                role = payload.get("role")
                tool = payload.get("name") if record_type in {"custom_tool_call", "function_call"} else None
                timestamp = row.get("timestamp") if isinstance(row, dict) else None
                records.append({
                    "file": str(path.relative_to(root)),
                    "line": line_number,
                    "timestamp": timestamp if isinstance(timestamp, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[0-9:.]+Z", timestamp) else "UNKNOWN",
                    "recordType": record_type if record_type in RECORD_TYPES else "other",
                    "role": role if role in ROLES else None,
                    "tool": tool if isinstance(tool, str) and SAFE_TOOL.fullmatch(tool) else None,
                })
    return {"filesScanned": files, "matches": matches, "malformedMatches": malformed_matches, "truncated": matches > len(records), "records": records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Explicit Codex session directory")
    parser.add_argument("--id", required=True, help="Exact non-secret RunId or evidence ID")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    try:
        result = scan_sessions(args.root, args.id, limit=args.limit)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
