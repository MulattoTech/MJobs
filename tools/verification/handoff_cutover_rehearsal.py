"""Read-only, source-pinned B3 cutover and rollback rehearsal.

No operation in this module migrates, overwrites, appends, or deletes the
canonical MJobs handoff. An in-memory simulated consumer demonstrates the
transition to reviewed summary bytes and recovery to the exact legacy bytes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from collections.abc import Sequence

from tools.verification.handoff_archive import _verify_archive
from tools.verification.handoff_current_summary import make_review_preview

_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class _InMemoryConsumer:
    """Synthetic consumer pointer; has no backing filesystem operations."""

    contents: bytes

    def switch_to(self, new_contents: bytes) -> None:
        self.contents = new_contents


def rehearsal(
    source: Path,
    snapshot: Path,
    reviewer_file: Path,
    expected_source_sha256: str,
) -> tuple[dict, str]:
    """Validate identical source/archive bytes and simulate exact rollback.

    The caller must explicitly pin source SHA to make stale source/selection
    errors detectable. The code neither chooses review entries nor treats a
    reviewer-authored status as independently established acceptance.
    """
    if not isinstance(expected_source_sha256, str) or not _SHA.fullmatch(
        expected_source_sha256
    ):
        raise ValueError("Expected source SHA-256 must be a full lowercase digest")
    source = Path(source)
    reviewer_file = Path(reviewer_file)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Source is missing or a symbolic link")
    if reviewer_file.is_symlink() or not reviewer_file.is_file():
        raise ValueError("Review input is missing or a symbolic link")
    if reviewer_file.stat().st_size > 65_536:
        raise ValueError("Review input exceeds maximum size")
    original = source.read_bytes()
    if _digest(original) != expected_source_sha256:
        raise ValueError("Source does not match explicit pre-migration pin")
    reconstructed, manifest = _verify_archive(snapshot)
    if reconstructed != original:
        raise ValueError("Immutable archive differs from original source")
    if manifest["source_sha256"] != expected_source_sha256:
        raise ValueError("Archive source digest does not match pin")

    reviewed_json = reviewer_file.read_bytes()
    review_meta, preview_text = make_review_preview(snapshot, reviewed_json)

    if source.read_bytes() != original or _digest(reviewed_json) != _digest(
        reviewer_file.read_bytes()
    ):
        raise ValueError("Source or reviewed input changed during rehearsal")

    consumer = _InMemoryConsumer(contents=original)
    consumer.switch_to(preview_text.encode("utf-8"))
    summary_digest = _digest(consumer.contents)
    if summary_digest != _digest(preview_text.encode("utf-8")):
        raise ValueError("Simulated cutover content mismatch")
    consumer.switch_to(reconstructed)
    if consumer.contents != original or _digest(consumer.contents) != expected_source_sha256:
        raise ValueError("Rollback did not restore exact source bytes")
    # A source, selection or archive modified during the simulated transition
    # cannot qualify as a completed rehearsal. Re-read all three authorities
    # after rollback instead of relying only on their pre-cutover fingerprints.
    if source.read_bytes() != original:
        raise ValueError("Source changed during rehearsal")
    if _digest(reviewer_file.read_bytes()) != _digest(reviewed_json):
        raise ValueError("Reviewed selection changed during rehearsal")
    stored_after, manifest_after = _verify_archive(snapshot)
    if (stored_after != original or
            manifest_after["source_sha256"] != expected_source_sha256):
        raise ValueError("Archive changed during rehearsal")

    return {
        "result": "MEMORY_ONLY_ROLLBACK_VERIFIED",
        "source_sha256": expected_source_sha256,
        "source_length": len(original),
        "checkpoint_count": manifest["checkpoint_count"],
        "review_sha256": review_meta["review_sha256"],
        "selected_count": review_meta["selected_count"],
        "preview_sha256": summary_digest,
        "rollback_sha256": _digest(consumer.contents),
        "review_authenticity": "NOT_VERIFIED_BY_THIS_TOOL",
        "semantic_accuracy": "NOT_VERIFIED_BY_THIS_TOOL",
        "canonical_cutover": "NOT_AUTHORIZED_OR_PERFORMED",
        "disk_writes": 0,
    }, preview_text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only, in-memory rollback rehearsal against a pinned snapshot"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--review-file", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument(
        "--render-reviewed-preview", action="store_true",
        help="Explicitly print reviewer-authored summary instead of content-free metadata",
    )
    args = parser.parse_args(argv)
    result, preview = rehearsal(
        args.source, args.snapshot, args.review_file, args.expected_source_sha256
    )
    if args.render_reviewed_preview:
        print(preview, end="")
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
