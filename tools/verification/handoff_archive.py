"""Opt-in, local-only immutable archive for a pinned MJobs handoff.

The default command is read-only. An archive can be written only to a
pre-existing directory outside the repository, with an explicit full source
digest. The canonical handoff is never modified.
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

from tools.verification.handoff_checkpoints import (
    build_verified_plan,
    recover_exact,
    split_handoff,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "planning" / "SESSION_HANDOFF.md"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9]{5}-[0-9a-f]{16}\Z")
_LOCK = ".mjobs-b3-archive.lock"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_exclusive(path: Path, content: bytes) -> None:
    """Create one new private file, flush it, and never replace existing bytes."""
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_dir(path: Path) -> None:
    """Best-effort directory durability where supported by the operating system."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _verify_archive(
    snapshot: Path, *, staged_digest: str | None = None
) -> tuple[bytes, dict]:
    """Re-read each saved file from disk and prove exact original reconstruction."""
    snapshot = Path(snapshot)
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("Snapshot directory missing or symbolic")
    root = snapshot.resolve(strict=True)
    if root.name.startswith("sha256-"):
        suffix = root.name.removeprefix("sha256-")
    elif staged_digest is not None:
        # Only the writer may verify an unpublished temporary directory.
        suffix = staged_digest
    else:
        raise ValueError("Snapshot directory name must contain its source digest")
    if not _SHA.fullmatch(suffix):
        raise ValueError("Invalid snapshot digest name")
    manifest_file = root / "manifest.json"
    pieces_dir = root / "pieces"
    if {entry.name for entry in root.iterdir()} != {"manifest.json", "pieces"}:
        raise ValueError("Unexpected archive root entries")
    if (manifest_file.is_symlink() or not manifest_file.is_file()
            or pieces_dir.is_symlink() or not pieces_dir.is_dir()):
        raise ValueError("Archive structure missing or symbolic")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("Invalid snapshot manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("source_sha256") != suffix:
        raise ValueError("Snapshot name and manifest digest disagree")
    entries = manifest.get("checkpoints")
    if not isinstance(entries, list):
        raise ValueError("Missing checkpoint inventory")

    content = []
    wanted: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("Invalid checkpoint metadata")
        digest = entry.get("sha256")
        checkpoint_id = entry.get("checkpoint_id")
        if (not isinstance(digest, str) or not _SHA.fullmatch(digest)
                or not isinstance(checkpoint_id, str)
                or not _ID.fullmatch(checkpoint_id)
                or checkpoint_id != f"{index:05d}-{digest[:16]}"):
            raise ValueError("Unsafe checkpoint identifier")
        name = checkpoint_id + ".bin"
        wanted.add(name)
        checkpoint = pieces_dir / name
        if checkpoint.is_symlink() or not checkpoint.is_file():
            raise ValueError("Missing or symbolic checkpoint file")
        content.append(checkpoint.read_bytes())

    actual = {p.name for p in pieces_dir.iterdir()}
    if actual != wanted:
        raise ValueError("Unexpected or missing checkpoint files")
    restored = recover_exact(content, manifest)
    if _sha256(restored) != suffix:
        raise ValueError("Archive checksum mismatch")
    return restored, manifest


def verify_archive(snapshot: Path) -> dict:
    """Return content-free verification metadata for one already saved snapshot."""
    recovered, manifest = _verify_archive(snapshot)
    return {
        "result": "ARCHIVE_VERIFIED",
        "source_sha256": manifest["source_sha256"],
        "source_length": len(recovered),
        "checkpoint_count": len(manifest["checkpoints"]),
    }


def _safe_root(destination: Path) -> Path:
    destination = Path(destination).expanduser()
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("Destination must be an existing non-symlink directory")
    root = destination.resolve(strict=True)
    repo = REPO_ROOT.resolve(strict=True)
    if root == repo or repo in root.parents:
        raise ValueError("Refusing to archive handoff content into the repository")
    # A second checkout can sit outside this checkout's directory. Refuse
    # destinations below any Git worktree, including linked worktrees whose
    # .git entry is a file rather than a directory.
    for ancestor in (root, *root.parents):
        marker = ancestor / ".git"
        if marker.exists() or marker.is_symlink():
            raise ValueError("Refusing archive destination inside a Git checkout")
    return root


def create_archive(source: Path, destination: Path, expected_sha256: str) -> dict:
    """Add an exact snapshot without ever changing or removing existing archives."""
    if not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256):
        raise ValueError("An explicit full lowercase SHA-256 source pin is required")
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Source must be an existing regular file")
    original = source.read_bytes()
    if _sha256(original) != expected_sha256:
        raise ValueError("Source SHA-256 differs from the expected pin")
    root = _safe_root(destination)
    final = root / ("sha256-" + expected_sha256)
    if final.is_symlink():
        raise ValueError("Refusing symbolic snapshot destination")
    if final.exists():
        recovered, manifest = _verify_archive(final)
        if recovered != original:
            raise ValueError("Existing snapshot does not match pinned source")
        return {
            "result": "ALREADY_VERIFIED",
            "source_sha256": expected_sha256,
            "source_length": len(recovered),
            "checkpoint_count": manifest["checkpoint_count"],
        }

    plan = build_verified_plan(original)
    segments = split_handoff(original)
    lock_path = root / _LOCK
    stage: Path | None = None
    lock_fd: int | None = None
    lock_owned = False
    try:
        # O_EXCL protects against another writer using this tool. An abandoned
        # lock is deliberately NOT auto-broken; it requires an owner review.
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            lock_owned = True
        except FileExistsError as exc:
            raise ValueError("Archive destination has an existing writer lock") from exc
        os.write(lock_fd, expected_sha256.encode("ascii"))
        os.fsync(lock_fd)
        os.close(lock_fd)
        lock_fd = None
        if final.exists() or final.is_symlink():
            raise ValueError("Snapshot destination appeared while acquiring lock")

        stage = root / (".staging-" + expected_sha256[:16] + "-" + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        pieces_dir = stage / "pieces"
        pieces_dir.mkdir(mode=0o700)
        for segment, entry in zip(segments, plan["checkpoints"], strict=True):
            checkpoint = pieces_dir / (entry["checkpoint_id"] + ".bin")
            _write_exclusive(checkpoint, segment.payload)
        _sync_dir(pieces_dir)
        _write_exclusive(
            stage / "manifest.json",
            (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )
        _sync_dir(stage)
        # Verify independently read staging bytes before atomic promotion.
        recovered, _ = _verify_archive(stage, staged_digest=expected_sha256)
        if recovered != original or source.read_bytes() != original:
            raise ValueError("Source changed or staged recovery differs")
        if final.exists() or final.is_symlink():
            raise ValueError("Snapshot destination already exists")
        # With the exclusive writer lock, no other compliant writer can
        # publish this digest concurrently. Rename preserves exact bytes.
        stage.rename(final)
        stage = None
        _sync_dir(root)
        recovered, manifest = _verify_archive(final)
        if recovered != original:
            raise ValueError("Published snapshot did not restore original bytes")
        return {
            "result": "SNAPSHOT_CREATED",
            "source_sha256": expected_sha256,
            "source_length": len(recovered),
            "checkpoint_count": manifest["checkpoint_count"],
        }
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        # Never remove another writer's lock after an O_EXCL failure.
        if lock_owned and lock_path.exists():
            lock_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan, verify or explicitly create a local immutable B3 handoff archive")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--expected-sha256")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true")
    group.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        if args.destination is None or args.expected_sha256 is None:
            parser.error("--write requires --destination and --expected-sha256")
        result = create_archive(args.source, args.destination, args.expected_sha256)
    elif args.verify:
        if args.destination is None:
            parser.error("--verify requires --destination pointing at one snapshot")
        result = verify_archive(args.destination)
    else:
        plan = build_verified_plan(args.source.read_bytes())
        result = {
            "result": "READ_ONLY_PLAN",
            "source_sha256": plan["source_sha256"],
            "source_length": plan["source_length"],
            "checkpoint_count": plan["checkpoint_count"],
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
