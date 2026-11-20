"""Read-only, fail-closed B3 handoff cutover readiness inventory.

This tool NEVER authorizes or performs a consumer switch. Its default mode
reads only local Git status and a SHA-256 digest of the legacy handoff. An
explicit full evidence check verifies a private archive/review/preview
triple without printing or writing any historical text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from collections.abc import Sequence

from tools.verification.handoff_archive import _safe_root, _LOCK as ARCHIVE_LOCK
from tools.verification.handoff_cutover_rehearsal import rehearsal
from tools.verification.handoff_private_preview import (
    _LOCK_NAME as PREVIEW_LOCK,
    verify_candidate,
)

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HANDOFF = "planning/SESSION_HANDOFF.md"
_MAX_HANDOFF_BYTES = 8_000_000
_MAX_WORKTREES = 64


def _git(root: Path, *args: str) -> str:
    """Read local Git state; no network, shell invocation or mutations."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Local Git state could not be verified") from exc
    if completed.returncode != 0:
        raise ValueError("Local Git state could not be verified")
    return completed.stdout.strip()


def _worktree_paths(root: Path) -> list[Path]:
    data = _git(root, "worktree", "list", "--porcelain")
    paths = []
    for line in data.splitlines():
        if line.startswith("worktree "):
            location = line.removeprefix("worktree ")
            if not location:
                raise ValueError("Worktree listing contains an empty location")
            paths.append(Path(location))
    if not paths or len(paths) > _MAX_WORKTREES:
        raise ValueError("Worktree inventory is empty or exceeds the bounded limit")
    if len({str(path).casefold() for path in paths}) != len(paths):
        raise ValueError("Worktree inventory contains duplicated paths")
    return paths


def scan_handoff_worktrees(
    repo: Path, independent_checkouts: Sequence[Path] | None = None
) -> dict:
    """Count dirty/unverifiable handoff owners without publishing path names."""
    repo = Path(repo).resolve(strict=True)
    checked = {
        "git_head": None,
        "git_branch": None,
        "source_sha256": None,
        "source_length": None,
        "worktree_count": None,
        "dirty_handoff_worktrees": 0,
        "unverifiable_worktrees": 0,
        "independent_checkout_count": (
            None if independent_checkouts is None else len(independent_checkouts)
        ),
        "dirty_independent_checkouts": 0,
        "unverifiable_independent_checkouts": 0,
        "independent_checkout_coverage": (
            "NOT_DECLARED" if independent_checkouts is None
            else "CALLER_DECLARED_NOT_AUTHENTICATED"
        ),
        "observations": [],
    }
    try:
        top = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != repo:
            checked["observations"].append("ROOT_IS_NOT_GIT_TOPLEVEL")
    except (ValueError, OSError):
        checked["observations"].append("ROOT_GIT_TOPLEVEL_UNVERIFIABLE")

    try:
        head = _git(repo, "rev-parse", "HEAD")
        if not _SHA.fullmatch(head):
            raise ValueError("Invalid Git HEAD")
        checked["git_head"] = head
        checked["git_branch"] = _git(repo, "branch", "--show-current")
        if checked["git_branch"] != "main":
            checked["observations"].append("SOURCE_CHECKOUT_NOT_ON_MAIN")
    except ValueError:
        checked["observations"].append("SOURCE_GIT_IDENTITY_UNVERIFIABLE")

    source = repo / _HANDOFF
    if source.is_symlink() or not source.is_file():
        checked["observations"].append("CANONICAL_HANDOFF_MISSING_OR_SYMBOLIC")
    else:
        try:
            if _git(repo, "ls-files", "--error-unmatch", "--", _HANDOFF) != _HANDOFF:
                raise ValueError("Git tracking unverified")
            size = source.stat().st_size
            if size > _MAX_HANDOFF_BYTES:
                raise ValueError("Handoff exceeds bounded inventory size")
            data = source.read_bytes()
            checked["source_sha256"] = hashlib.sha256(data).hexdigest()
            checked["source_length"] = len(data)
        except (OSError, ValueError):
            checked["observations"].append("CANONICAL_HANDOFF_UNVERIFIABLE")

    registered: set[str] = set()
    try:
        paths = _worktree_paths(repo)
        registered = {str(path.resolve()).casefold() for path in paths}
        checked["worktree_count"] = len(paths)
        for path in paths:
            if path.is_symlink() or not path.is_dir():
                checked["unverifiable_worktrees"] += 1
                continue
            try:
                state = _git(
                    path, "status", "--porcelain=v1", "--untracked-files=normal",
                    "--", _HANDOFF,
                )
                if state:
                    checked["dirty_handoff_worktrees"] += 1
            except ValueError:
                checked["unverifiable_worktrees"] += 1
        if checked["dirty_handoff_worktrees"]:
            checked["observations"].append("UNCOMMITTED_HANDOFF_IN_WORKTREE")
        if checked["unverifiable_worktrees"]:
            checked["observations"].append("WORKTREE_HANDOFF_STATUS_UNVERIFIABLE")
    except ValueError:
        checked["observations"].append("WORKTREE_INVENTORY_UNVERIFIABLE")

    # Git's worktree list does not include independent Git clones, even
    # those on the same machine and owned by a different active writer.
    # Never assert complete writer coverage without an explicit declaration.
    if independent_checkouts is None:
        checked["observations"].append("INDEPENDENT_CHECKOUTS_NOT_ENUMERATED")
    elif len(independent_checkouts) > 32:
        checked["observations"].append("INDEPENDENT_CHECKOUT_LIST_TOO_LARGE")
    else:
        inspected: set[str] = set()
        for independent in independent_checkouts:
            try:
                other = Path(independent)
                if other.is_symlink() or not other.is_dir():
                    raise ValueError("Independent checkout missing or symbolic")
                other = other.resolve(strict=True)
                identity = str(other).casefold()
                if identity in registered or identity in inspected:
                    raise ValueError("Independent checkout duplicated")
                inspected.add(identity)
                if Path(_git(other, "rev-parse", "--show-toplevel")).resolve(strict=True) != other:
                    raise ValueError("Independent checkout is not its Git root")
                if _git(other, "ls-files", "--error-unmatch", "--", _HANDOFF) != _HANDOFF:
                    raise ValueError("Independent checkout handoff untracked")
                status = _git(
                    other, "status", "--porcelain=v1", "--untracked-files=normal",
                    "--", _HANDOFF,
                )
                if status:
                    checked["dirty_independent_checkouts"] += 1
            except (ValueError, OSError):
                checked["unverifiable_independent_checkouts"] += 1
        if checked["dirty_independent_checkouts"]:
            checked["observations"].append("UNCOMMITTED_HANDOFF_IN_INDEPENDENT_CHECKOUT")
        if checked["unverifiable_independent_checkouts"]:
            checked["observations"].append("INDEPENDENT_CHECKOUT_STATUS_UNVERIFIABLE")
    checked["observations"] = sorted(set(checked["observations"]))
    return checked


def _validate_pin(value: str | None, label: str, length: int = 64) -> str:
    pattern = _SHA256 if length == 64 else _SHA
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Invalid full lowercase {label}")
    return value


def _private_inputs(snapshot: Path, review_file: Path, candidate: Path) -> None:
    """Require all evidence paths to be outside Git, without disclosing paths."""
    snapshot = Path(snapshot)
    review_file = Path(review_file)
    candidate = Path(candidate)
    if snapshot.is_symlink() or candidate.is_symlink() or review_file.is_symlink():
        raise ValueError("Private evidence paths may not be symlinks")
    if not review_file.is_file():
        raise ValueError("Private reviewed selection is unavailable")
    _safe_root(snapshot.parent)
    _safe_root(review_file.parent)
    _safe_root(candidate.parent)
    archive_root = snapshot.resolve(strict=True)
    candidate_root = candidate.resolve(strict=True)
    review_path = review_file.resolve(strict=True)
    if archive_root == candidate_root or archive_root in candidate_root.parents:
        raise ValueError("Inactive candidate must not be nested in its source archive")
    if archive_root in review_path.parents:
        raise ValueError("Reviewed selection must not mutate the source archive")
    if (snapshot.parent / ARCHIVE_LOCK).exists():
        raise ValueError("An archive writer lock is present")
    if (candidate.parent / PREVIEW_LOCK).exists():
        raise ValueError("A private preview writer lock is present")
    if any(item.name.startswith(".staging-") for item in snapshot.parent.iterdir()):
        raise ValueError("Unreviewed archive staging directory is present")
    if any(item.name.startswith(".preview-stage-") for item in candidate.parent.iterdir()):
        raise ValueError("Unreviewed preview staging directory is present")


def readiness_report(
    repo: Path,
    *,
    independent_checkouts: Sequence[Path] | None = None,
    expected_head: str | None = None,
    expected_source_sha256: str | None = None,
    snapshot: Path | None = None,
    review_file: Path | None = None,
    expected_review_sha256: str | None = None,
    candidate: Path | None = None,
    expected_preview_sha256: str | None = None,
) -> dict:
    """Report technical preconditions without ever granting cutover authority.

    Full mode requires all six pins and paths. No source or reviewer text,
    private paths or worktree names are present in the result.
    """
    # Freeze the caller's declared inventory for preflight and postflight.
    # A mutable list changing while verification runs is not an authority.
    checkouts = (tuple(independent_checkouts)
                 if independent_checkouts is not None else None)
    scan = scan_handoff_worktrees(repo, checkouts)
    full_inputs = (
        expected_head, expected_source_sha256, snapshot, review_file,
        expected_review_sha256, candidate, expected_preview_sha256,
    )
    any_input = any(value is not None for value in full_inputs)
    missing = any(value is None for value in full_inputs)
    result = {
        "result": "B3_CUTOVER_READINESS_ONLY",
        "mode": "FULL_EVIDENCE" if any_input else "WORKTREE_SCAN",
        "source_git_head": scan["git_head"],
        "source_git_branch": scan["git_branch"],
        "source_sha256": scan["source_sha256"],
        "source_length": scan["source_length"],
        "worktree_count": scan["worktree_count"],
        "dirty_handoff_worktrees": scan["dirty_handoff_worktrees"],
        "unverifiable_worktrees": scan["unverifiable_worktrees"],
        "independent_checkout_count": scan["independent_checkout_count"],
        "dirty_independent_checkouts": scan["dirty_independent_checkouts"],
        "unverifiable_independent_checkouts": scan["unverifiable_independent_checkouts"],
        "independent_checkout_coverage": scan["independent_checkout_coverage"],
        "technical_evidence": "NOT_QUERIED",
        "preview_sha256": None,
        "review_sha256": None,
        "legacy_rollback_sha256": None,
        "remote_protection": "NOT_QUERIED",
        "review_authenticity": "NOT_VERIFIED",
        "semantic_current_state": "NOT_VERIFIED",
        "single_writer_authority": "NOT_ESTABLISHED",
        "canonical_cutover": "NOT_AUTHORIZED_OR_PERFORMED",
        "can_cutover": False,
        "blockers": scan["observations"][:],
        "remaining_human_gates": [
            "VERIFY_INDEPENDENT_REVIEWER_AND_CURRENT_FACTS",
            "RECONCILE_AND_EXCLUSIVELY_ASSIGN_ALL_ACTIVE_HANDOFF_WRITERS",
            "VERIFY_CURRENT_REMOTE_PROTECTED_CI_AND_EXPLICIT_CUTOVER_APPROVAL",
            "DESIGN_AND_VALIDATE_ACTUAL_DURABLE_READER_TRANSITION_ROLLBACK",
        ],
    }
    if not any_input:
        result["blockers"].append("PRIVATE_EVIDENCE_NOT_SUPPLIED")
    elif missing:
        result["blockers"].append("INCOMPLETE_PINNED_PRIVATE_EVIDENCE")
    else:
        try:
            _validate_pin(expected_head, "Git HEAD", 40)
            _validate_pin(expected_source_sha256, "source SHA-256")
            _validate_pin(expected_review_sha256, "review SHA-256")
            _validate_pin(expected_preview_sha256, "preview SHA-256")
            if scan["git_head"] != expected_head:
                raise ValueError("Expected local Git HEAD does not match")
            if scan["source_sha256"] != expected_source_sha256:
                raise ValueError("Source handoff digest differs from source pin")
            _private_inputs(snapshot, review_file, candidate)
            reviewed_bytes = Path(review_file).read_bytes()
            if hashlib.sha256(reviewed_bytes).hexdigest() != expected_review_sha256:
                raise ValueError("Reviewed selection digest does not match independent pin")
            replay, text = rehearsal(
                Path(repo) / _HANDOFF, snapshot, review_file, expected_source_sha256
            )
            if replay["review_sha256"] != expected_review_sha256:
                raise ValueError("Rehearsal selected a different review source")
            if replay["preview_sha256"] != expected_preview_sha256:
                raise ValueError("Rehearsal preview digest does not match pin")
            verified = verify_candidate(
                candidate, expected_preview_sha256=expected_preview_sha256,
                expected_source_sha256=expected_source_sha256,
                expected_review_sha256=expected_review_sha256,
            )
            if verified["preview_length"] != len(text.encode("utf-8")):
                raise ValueError("Saved private preview length differs from rehearsal")
            result["technical_evidence"] = "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
            result["preview_sha256"] = verified["preview_sha256"]
            result["review_sha256"] = verified["review_sha256"]
            result["legacy_rollback_sha256"] = replay["rollback_sha256"]
        except (OSError, ValueError, UnicodeError):
            result["technical_evidence"] = "FAILED"
            result["blockers"].append("PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE")
        # The first worktree scan predates the expensive archive/review
        # verification. If a handoff writer, Git HEAD or registered checkout
        # changes while that runs, its initial clean state is stale. This
        # second scan is a detection barrier, not a process lock.
        try:
            postflight = scan_handoff_worktrees(repo, checkouts)
            if postflight != scan:
                result["technical_evidence"] = "FAILED"
                result["blockers"].append(
                    "HANDOFF_OR_CHECKOUT_STATE_CHANGED_DURING_VERIFICATION"
                )
                result["blockers"].extend(postflight["observations"])
        except (OSError, ValueError):
            result["technical_evidence"] = "FAILED"
            result["blockers"].append("POSTFLIGHT_HANDOFF_STATUS_UNVERIFIABLE")
        # The initial reviewer/archive/candidate proof can also become stale
        # after its first check. Require the same independent pins to verify
        # again before reporting an internally consistent technical review.
        if result["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT":
            try:
                _private_inputs(snapshot, review_file, candidate)
                review_after = Path(review_file).read_bytes()
                if hashlib.sha256(review_after).hexdigest() != expected_review_sha256:
                    raise ValueError("Reviewer selection changed during verification")
                replay_after, text_after = rehearsal(
                    Path(repo) / _HANDOFF, snapshot, review_file,
                    expected_source_sha256,
                )
                if (replay_after["review_sha256"] != expected_review_sha256
                        or replay_after["preview_sha256"] != expected_preview_sha256
                        or replay_after["rollback_sha256"] != expected_source_sha256):
                    raise ValueError("Pinned archive or summary changed during verification")
                candidate_after = verify_candidate(
                    candidate, expected_preview_sha256=expected_preview_sha256,
                    expected_source_sha256=expected_source_sha256,
                    expected_review_sha256=expected_review_sha256,
                )
                if candidate_after["preview_length"] != len(text_after.encode("utf-8")):
                    raise ValueError("Private candidate changed during verification")
            except (OSError, ValueError, UnicodeError):
                result["technical_evidence"] = "FAILED"
                result["blockers"].append(
                    "PINNED_PRIVATE_EVIDENCE_CHANGED_DURING_VERIFICATION"
                )
    result["blockers"] = sorted(set(result["blockers"]))
    result["ready_for_review"] = (
        not result["blockers"]
        and result["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
    )
    # This program never makes the independent semantic / owner / release
    # decision, even with all synthetic technical checks passing.
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only B3 readiness report; never activate a handoff reader"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    coverage = parser.add_mutually_exclusive_group()
    coverage.add_argument("--independent-checkout", type=Path, action="append",
                          help="Read-only inventory of another independent Git clone")
    coverage.add_argument("--attest-no-independent-checkouts", action="store_true",
                          help="Declare no other clones are known; not proof of global coverage")
    parser.add_argument("--expected-head")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--expected-review-sha256")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--expected-preview-sha256")
    args = parser.parse_args(argv)
    report = readiness_report(
        args.root,
        independent_checkouts=(
            [] if args.attest_no_independent_checkouts else args.independent_checkout
        ),
        expected_head=args.expected_head,
        expected_source_sha256=args.expected_source_sha256,
        snapshot=args.snapshot,
        review_file=args.review_file,
        expected_review_sha256=args.expected_review_sha256,
        candidate=args.candidate,
        expected_preview_sha256=args.expected_preview_sha256,
    )
    print(json.dumps(report, sort_keys=True))
    # A scan that finds blockers still reports them as metadata; its exit
    # code must not accidentally imply that a real cutover was permitted.
    return 0 if report["ready_for_review"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
