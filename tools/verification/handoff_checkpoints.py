"""Read-only, byte-exact characterization for B3 handoff migration.

This module does not rewrite the canonical handoff or publish checkpoints.
It builds a deterministic inventory and proves that independently retained
section bytes can reconstruct the entire original input without loss.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from collections.abc import Sequence

_HEADING = re.compile(rb"^(#{1,2})[ \t]+(.+?)\s*$")
_FENCE = re.compile(rb"^ {0,3}((?:\x60{3,}|\x7e{3,}))")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HandoffSection:
    """One exact byte range; the payload is kept only in process memory."""

    start: int
    payload: bytes
    heading_level: int | None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def _fence_token(line: bytes) -> bytes | None:
    match = _FENCE.match(line)
    return match.group(1) if match else None


def split_handoff(source: bytes) -> tuple[HandoffSection, ...]:
    """Split on H1/H2 ATX headings outside fenced blocks, preserving bytes."""
    if not isinstance(source, bytes):
        raise TypeError("Source must be raw bytes")
    result: list[HandoffSection] = []
    start = position = 0
    current_level: int | None = None
    fence: bytes | None = None

    for line in source.splitlines(keepends=True):
        logical = line.rstrip(b"\r\n")
        token = _fence_token(logical)
        if fence is not None:
            # An ATX-looking line inside code is always content, not a break.
            if (
                token is not None
                and token[:1] == fence[:1]
                and len(token) >= len(fence)
                and not logical[logical.index(token) + len(token):].strip()
            ):
                fence = None
        elif token is not None:
            fence = token
        else:
            heading = _HEADING.match(logical)
            if heading:
                if position > start:
                    result.append(HandoffSection(start, source[start:position], current_level))
                start = position
                current_level = len(heading.group(1))
        position += len(line)

    if position != len(source):
        raise ValueError("Line inventory did not cover the original bytes")
    if start < len(source):
        result.append(HandoffSection(start, source[start:], current_level))
    return tuple(result)


def manifest_for(source: bytes, sections: Sequence[HandoffSection]) -> dict:
    """Return metadata only; never include historical checkpoint content."""
    entries = []
    for ordinal, section in enumerate(sections):
        digest = section.sha256
        entries.append({
            "ordinal": ordinal,
            "start": section.start,
            "length": len(section.payload),
            "sha256": digest,
            "heading_level": section.heading_level,
            "checkpoint_id": f"{ordinal:05d}-{digest[:16]}",
        })
    return {
        "schema_version": 1,
        "mode": "READ_ONLY_MIGRATION_INVENTORY",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_length": len(source),
        "checkpoint_count": len(entries),
        "checkpoints": entries,
    }


def recover_exact(checkpoint_bytes: Sequence[bytes], manifest: dict) -> bytes:
    """Verify independently supplied checkpoint bytes and restore exact source.

    Invalid/missing/reordered/corrupted checkpoints fail closed. No writing
    occurs, so an operator can compare recovery to the original before moving
    a single canonical file.
    """
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported checkpoint schema")
    entries = manifest.get("checkpoints")
    if not isinstance(entries, list) or len(entries) != len(checkpoint_bytes):
        raise ValueError("Checkpoint count mismatch")
    if manifest.get("checkpoint_count") != len(entries):
        raise ValueError("Manifest count mismatch")

    position = 0
    restored: list[bytes] = []
    for index, (entry, payload) in enumerate(zip(entries, checkpoint_bytes, strict=True)):
        if not isinstance(entry, dict) or not isinstance(payload, bytes):
            raise ValueError("Invalid checkpoint type")
        digest = hashlib.sha256(payload).hexdigest()
        if (entry.get("ordinal") != index
                or entry.get("start") != position
                or entry.get("length") != len(payload)
                or entry.get("sha256") != digest
                or entry.get("checkpoint_id") != f"{index:05d}-{digest[:16]}"):
            raise ValueError(f"Checkpoint identity or offset mismatch at {index}")
        if entry.get("heading_level") not in (None, 1, 2):
            raise ValueError("Unknown heading level")
        restored.append(payload)
        position += len(payload)

    data = b"".join(restored)
    if (manifest.get("source_length") != len(data)
            or manifest.get("source_sha256") != hashlib.sha256(data).hexdigest()):
        raise ValueError("Source recovery digest mismatch")
    parsed = split_handoff(data)
    if (len(parsed) != len(entries)
            or any(section.start != entry["start"]
                   or section.heading_level != entry["heading_level"]
                   for section, entry in zip(parsed, entries, strict=True))):
        raise ValueError("Checkpoint segmentation mismatch")
    return data


def build_verified_plan(source: bytes) -> dict:
    sections = split_handoff(source)
    plan = manifest_for(source, sections)
    if recover_exact([section.payload for section in sections], plan) != source:
        raise ValueError("Byte-exact recovery failed")
    return plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan a lossless B3 handoff migration without writing any files")
    parser.add_argument(
        "--source", type=Path, default=_REPOSITORY_ROOT / "planning" / "SESSION_HANDOFF.md",
        help="Input to inspect (read-only; does not modify or publish content)")
    parser.add_argument("--json", action="store_true", help="Print metadata-only JSON inventory")
    args = parser.parse_args(argv)
    source = args.source.read_bytes()
    plan = build_verified_plan(source)
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(f"Handoff recovery inventory: PASS; "
              f"bytes={plan['source_length']}; "
              f"sections={plan['checkpoint_count']}; "
              f"source_sha256={plan['source_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
