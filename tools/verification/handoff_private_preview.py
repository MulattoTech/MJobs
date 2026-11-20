"""Opt-in private B3 review-preview artifact, with no reader activation.

Default execution only validates input and prints content-free metadata.
An explicit --write creates one immutable, source-bound preview directory
OUTSIDE all Git worktrees. This module never changes the canonical handoff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from collections.abc import Sequence

from tools.verification.handoff_archive import _safe_root, _sync_dir
from tools.verification.handoff_cutover_rehearsal import rehearsal

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_PREFIX = "review-preview-sha256-"
_LOCK_NAME = ".mjobs-b3-preview.lock"
_FIELDS = frozenset({
    "schema_version", "source_sha256", "review_sha256", "preview_sha256",
    "preview_length", "selected_count", "canonical_cutover",
})


def _digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _write_exclusive(path: Path, contents: bytes) -> None:
    """Create private data with restrictive mode before writing any bytes."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"{field} requires a full lowercase SHA-256 digest")
    return value


def _candidate_name(digest: str) -> str:
    return _PREFIX + _sha(digest, "preview SHA-256")


def _content_free_plan(
    source: Path, snapshot: Path, reviewed_file: Path, source_sha: str
) -> tuple[dict, bytes]:
    result, text = rehearsal(source, snapshot, reviewed_file, _sha(source_sha, "source SHA-256"))
    payload = text.encode("utf-8")
    if _digest(payload) != result["preview_sha256"]:
        raise ValueError("Rehearsal generated an inconsistent preview digest")
    receipt = {
        "schema_version": 1,
        "source_sha256": result["source_sha256"],
        "review_sha256": result["review_sha256"],
        "preview_sha256": result["preview_sha256"],
        "preview_length": len(payload),
        "selected_count": result["selected_count"],
        "canonical_cutover": "NOT_AUTHORIZED_OR_PERFORMED",
    }
    return receipt, payload


def verify_candidate(
    directory: Path, *, expected_preview_sha256: str,
    expected_source_sha256: str | None = None,
) -> dict:
    """Verify independently reloaded private bytes; never print review content."""
    digest = _sha(expected_preview_sha256, "expected preview SHA-256")
    if expected_source_sha256 is not None:
        _sha(expected_source_sha256, "expected source SHA-256")
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Candidate directory missing or symbolic")
    root = directory.resolve(strict=True)
    if root.name != _candidate_name(digest):
        raise ValueError("Candidate directory name disagrees with pinned preview SHA-256")
    if {x.name for x in root.iterdir()} != {"preview.md", "receipt.json"}:
        raise ValueError("Candidate has unexpected or missing entries")
    preview_file, receipt_file = root / "preview.md", root / "receipt.json"
    if (preview_file.is_symlink() or receipt_file.is_symlink()
            or not preview_file.is_file() or not receipt_file.is_file()):
        raise ValueError("Candidate contains missing or symbolic files")
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Candidate receipt is not valid JSON") from exc
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise ValueError("Candidate receipt has missing or unknown fields")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise ValueError("Unsupported preview receipt schema")
    if receipt["canonical_cutover"] != "NOT_AUTHORIZED_OR_PERFORMED":
        raise ValueError("Candidate may not claim an active cutover")
    for field in ("source_sha256", "review_sha256", "preview_sha256"):
        _sha(receipt[field], field)
    if receipt["preview_sha256"] != digest:
        raise ValueError("Preview receipt does not match pinned digest")
    if expected_source_sha256 is not None and receipt["source_sha256"] != expected_source_sha256:
        raise ValueError("Candidate does not match expected source SHA-256")
    if type(receipt["selected_count"]) is not int or not 1 <= receipt["selected_count"] <= 128:
        raise ValueError("Invalid reviewed selection count")
    if type(receipt["preview_length"]) is not int or receipt["preview_length"] < 1:
        raise ValueError("Invalid preview length")
    data = preview_file.read_bytes()
    if len(data) != receipt["preview_length"] or _digest(data) != digest:
        raise ValueError("Private preview bytes are missing or corrupted")
    try:
        decoded = data.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Preview bytes are not UTF-8") from exc
    if not decoded.startswith("# MJobs Current Handoff"):
        raise ValueError("Unrecognized preview header")
    return {
        "result": "PRIVATE_PREVIEW_VERIFIED_NOT_ACTIVATED",
        "source_sha256": receipt["source_sha256"],
        "review_sha256": receipt["review_sha256"],
        "preview_sha256": digest,
        "preview_length": len(data),
        "selected_count": receipt["selected_count"],
        "canonical_cutover": receipt["canonical_cutover"],
    }


def create_candidate(
    source: Path, snapshot: Path, reviewed_file: Path,
    source_sha: str, destination: Path,
) -> dict:
    """Write one copy-once preview; never touch the original source or reader."""
    receipt, payload = _content_free_plan(source, snapshot, reviewed_file, source_sha)
    root = _safe_root(destination)
    # The manifest and sections in an archive are immutable. A new preview
    # must never be placed inside the archive snapshot, even if not in Git.
    snapshot_path = Path(snapshot).resolve(strict=True)
    if root == snapshot_path or snapshot_path in root.parents:
        raise ValueError("Candidate destination is inside the source archive")
    final = root / _candidate_name(receipt["preview_sha256"])
    if final.is_symlink():
        raise ValueError("Candidate destination is symbolic")
    if final.exists():
        verified = verify_candidate(
            final, expected_preview_sha256=receipt["preview_sha256"],
            expected_source_sha256=receipt["source_sha256"],
        )
        if verified["review_sha256"] != receipt["review_sha256"]:
            raise ValueError("Existing candidate binds a different reviewer selection")
        return dict(verified, result="EXISTING_PRIVATE_PREVIEW_VERIFIED")

    lock_path = root / _LOCK_NAME
    stage: Path | None = None
    lock_fd: int | None = None
    owned_lock = False
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            owned_lock = True
        except FileExistsError as exc:
            raise ValueError("Existing preview writer lock must be reviewed") from exc
        os.write(lock_fd, receipt["preview_sha256"].encode("ascii"))
        os.fsync(lock_fd)
        os.close(lock_fd)
        lock_fd = None
        if final.exists() or final.is_symlink():
            raise ValueError("Candidate directory appeared while acquiring lock")
        stage = root / (".preview-stage-" + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        _write_exclusive(stage / "preview.md", payload)
        _write_exclusive(
            stage / "receipt.json",
            (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
        _sync_dir(stage)

        # The staging name intentionally differs from its published digest.
        # Independently read the exact bytes before renaming.
        if (stage / "preview.md").read_bytes() != payload:
            raise ValueError("Staged preview readback differs")
        reread = json.loads((stage / "receipt.json").read_text(encoding="utf-8"))
        if reread != receipt:
            raise ValueError("Staged receipt readback differs")
        if _digest((stage / "preview.md").read_bytes()) != receipt["preview_sha256"]:
            raise ValueError("Staged preview SHA-256 differs")
        # Recheck the complete source/archive/review pair before publication.
        current, refreshed = _content_free_plan(source, snapshot, reviewed_file, source_sha)
        if current != receipt or refreshed != payload:
            raise ValueError("Pinned source, review or archive changed while staging")
        if final.exists() or final.is_symlink():
            raise ValueError("Candidate destination appeared during staging")
        stage.rename(final)
        stage = None
        _sync_dir(root)
        verified = verify_candidate(
            final, expected_preview_sha256=receipt["preview_sha256"],
            expected_source_sha256=receipt["source_sha256"],
        )
        if verified["review_sha256"] != receipt["review_sha256"]:
            raise ValueError("Published candidate review digest mismatch")
        return dict(verified, result="PRIVATE_PREVIEW_CREATED_NOT_ACTIVATED")
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        if owned_lock and lock_path.exists():
            lock_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare or verify a private B3 preview; never activate any reader"
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--expected-preview-sha256")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true")
    group.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if args.verify:
        if any(x is not None for x in (
            args.source, args.snapshot, args.review_file, args.destination
        )):
            parser.error("--verify cannot accept source, snapshot, review or destination flags")
        if args.candidate is None or args.expected_preview_sha256 is None:
            parser.error("--verify requires --candidate and --expected-preview-sha256")
        result = verify_candidate(
            args.candidate, expected_preview_sha256=args.expected_preview_sha256,
            expected_source_sha256=args.expected_source_sha256,
        )
    else:
        if args.candidate is not None or args.expected_preview_sha256 is not None:
            parser.error("Candidate verification flags require --verify")
        if args.destination is not None and not args.write:
            parser.error("--destination requires explicit --write")
        if not all(x is not None for x in (
            args.source, args.snapshot, args.review_file, args.expected_source_sha256
        )):
            parser.error("Planning or writing requires source, snapshot, review and source SHA-256")
        if args.write:
            if args.destination is None:
                parser.error("--write requires an existing --destination directory")
            result = create_candidate(
                args.source, args.snapshot, args.review_file,
                args.expected_source_sha256, args.destination,
            )
        else:
            receipt, _ = _content_free_plan(
                args.source, args.snapshot, args.review_file,
                args.expected_source_sha256,
            )
            result = dict(receipt, result="READ_ONLY_PRIVATE_PREVIEW_PLAN")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
