"""Strict, read-only preview of a manually reviewed B3 current handoff.

Never selects a supposedly latest historical checkpoint by timestamp or order.
Never writes a generated summary or modifies canonical acceptance authorities.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from collections.abc import Sequence

from tools.verification.handoff_archive import _verify_archive

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = frozenset({"ACTIVE", "BLOCKED", "COMPLETE", "UNKNOWN"})
_TOP_KEYS = frozenset({
    "schema_version", "source_sha256", "reviewed_by", "reviewed_at_utc",
    "review_reference", "entries",
})
_ENTRY_KEYS = frozenset({
    "subject", "checkpoint_id", "status", "summary", "evidence", "unknowns",
})
_MAX_REVIEW_BYTES = 65_536
_MAX_ENTRIES = 128
_BIDI_CONTROLS = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


def _line(value: object, field: str, max_length: int) -> str:
    """Require one bounded, unambiguous line of reviewer-authored plain text."""
    if (not isinstance(value, str) or not value or len(value) > max_length
            or value != value.strip() or any(
                ord(c) < 32 or ord(c) == 127 or ord(c) in _BIDI_CONTROLS
                for c in value
            )):
        raise ValueError(f"Invalid or unbounded {field}")
    return value


def _safe_markdown(value: str) -> str:
    """Prevent reviewer text from injecting headings, tables or HTML markup."""
    meta = set("\\*_[]|<>#{}")
    return "".join("\\" + ch if ch in meta or ch == chr(96) else ch
                   for ch in value)


def _review_object(review_bytes: bytes) -> dict:
    if not isinstance(review_bytes, bytes) or len(review_bytes) > _MAX_REVIEW_BYTES:
        raise ValueError("Review record exceeds bounded input size")
    try:
        payload = json.loads(review_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Invalid UTF-8 review JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        raise ValueError("Review record has missing or unknown fields")
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported current-review schema")
    if not isinstance(payload.get("source_sha256"), str) or not _SHA.fullmatch(
        payload["source_sha256"]
    ):
        raise ValueError("Invalid review source digest")
    _line(payload["reviewed_by"], "reviewed_by", 128)
    _line(payload["review_reference"], "review_reference", 250)
    date = _line(payload["reviewed_at_utc"], "reviewed_at_utc", 20)
    try:
        datetime.strptime(date, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("Review time must be explicit UTC") from exc
    entries = payload.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_ENTRIES:
        raise ValueError("Review requires a bounded nonempty selection")
    return payload


def make_review_preview(snapshot: Path, reviewed_json: bytes) -> tuple[dict, str]:
    """Validate a pinned archive and render only explicit reviewed claims."""
    restored, archive = _verify_archive(snapshot)
    review = _review_object(reviewed_json)
    if review["source_sha256"] != archive["source_sha256"]:
        raise ValueError("Review and immutable archive refer to different sources")
    if len(restored) != archive["source_length"]:
        raise ValueError("Archive restoration length mismatch")
    available = {record["checkpoint_id"] for record in archive["checkpoints"]}
    seen: set[str] = set()
    entries: list[dict] = []
    for index, item in enumerate(review["entries"]):
        if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
            raise ValueError(f"Review item {index} has missing or unknown fields")
        subject = _line(item["subject"], "subject", 100)
        folded = subject.casefold()
        if folded in seen:
            raise ValueError("Duplicate reviewed subject")
        seen.add(folded)
        checkpoint = _line(item["checkpoint_id"], "checkpoint_id", 40)
        if checkpoint not in available:
            raise ValueError("Selection references an unknown checkpoint")
        status = _line(item["status"], "status", 16)
        if status not in _STATUSES:
            raise ValueError("Unsupported reviewed status")
        entries.append({
            "subject": subject,
            "checkpoint_id": checkpoint,
            "status": status,
            "summary": _line(item["summary"], "summary", 500),
            "evidence": _line(item["evidence"], "evidence", 250),
            "unknowns": _line(item["unknowns"], "unknowns", 250),
        })

    review_digest = hashlib.sha256(reviewed_json).hexdigest()
    meta = {
        "result": "REVIEWED_PREVIEW_NOT_CANONICAL",
        "source_sha256": archive["source_sha256"],
        "checkpoint_count": archive["checkpoint_count"],
        "selected_count": len(entries),
        "review_sha256": review_digest,
        "reviewed_at_utc": review["reviewed_at_utc"],
    }
    output = [
        "# MJobs Current Handoff — Reviewed Preview",
        "",
        "**NOT canonical acceptance, production readiness, or a replacement for the full archive.**",
        "",
        "Pinned legacy source SHA-256: " + archive["source_sha256"],
        "Reviewed selection SHA-256: " + review_digest,
        "Reviewed by: " + _safe_markdown(review["reviewed_by"]),
        "Review time (UTC): " + review["reviewed_at_utc"],
        "Review reference: " + _safe_markdown(review["review_reference"]),
        "",
        "The subjects and statuses below were supplied by the reviewer, not inferred",
        "by this generator. Each references an immutable checkpoint, but this",
        "tool does not independently establish semantic accuracy or PASS acceptance.",
        "",
    ]
    for item in entries:
        output.extend([
            "## " + _safe_markdown(item["subject"]),
            "",
            "Status: " + item["status"],
            "Checkpoint ID: " + item["checkpoint_id"],
            "Reviewed summary: " + _safe_markdown(item["summary"]),
            "Reviewer evidence: " + _safe_markdown(item["evidence"]),
            "Known unknowns: " + _safe_markdown(item["unknowns"]),
            "",
        ])
    return meta, "\n".join(output).rstrip() + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate or render a reviewer-pinned current handoff preview")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--review-file", type=Path, required=True)
    parser.add_argument("--render", action="store_true",
                        help="Explicitly print reviewer-written summary text")
    args = parser.parse_args(argv)
    if args.review_file.is_symlink() or not args.review_file.is_file():
        parser.error("Review input must be a regular non-symlink file")
    if args.review_file.stat().st_size > _MAX_REVIEW_BYTES:
        parser.error("Review input exceeds permitted size")
    metadata, summary = make_review_preview(
        args.snapshot, args.review_file.read_bytes())
    if args.render:
        print(summary, end="")
    else:
        print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
