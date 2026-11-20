"""Read-only metadata inventory of B3 handoff versions across local checkouts.

No handoff text, private paths, or owner identities are emitted. This module
cannot authorize cutover, overwrite a file, or establish exclusive ownership.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
from collections.abc import Sequence

from tools.verification.handoff_cutover_readiness import (
    _HANDOFF, _git, _handoff_digest, _worktree_paths,
    scan_handoff_worktrees,
)

_GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_MAX_INDEPENDENT = 32


def _git_oid(value: str) -> str:
    if not _GIT_OID.fullmatch(value):
        raise ValueError("Git object ID is missing or malformed")
    return value


def _head_relationship(checkout: Path, checkout_head: str, main_head: str) -> str:
    """Read-only local ancestry check; missing commits are not fetched."""
    if checkout_head == main_head:
        return "EXACT_MAIN"

    def is_ancestor(older: str, newer: str) -> int | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(checkout), "merge-base", "--is-ancestor",
                 older, newer],
                capture_output=True, text=True, check=False, timeout=15,
            )
            return result.returncode
        except (OSError, subprocess.TimeoutExpired):
            return None

    earlier = is_ancestor(checkout_head, main_head)
    if earlier == 0:
        return "ANCESTOR_OF_MAIN"
    if earlier != 1:
        return "UNVERIFIABLE"
    later = is_ancestor(main_head, checkout_head)
    if later == 0:
        return "DESCENDANT_OF_MAIN"
    if later == 1:
        return "DIVERGED_FROM_MAIN"
    return "UNVERIFIABLE"


def _inspect(
    checkout: Path, kind: str, ordinal: int,
    main_sha: str, main_blob: str, main_head: str,
) -> dict:
    """Inspect tracked source evidence without returning path or raw text."""
    item = {
        "checkout_ref": f"{kind}-{ordinal:02d}",
        "kind": kind,
        "git_head": None,
        "head_relationship": "UNVERIFIABLE",
        "committed_blob": None,
        "working_sha256": None,
        "git_status": "UNVERIFIABLE",
        "classification": "UNVERIFIABLE",
    }
    try:
        if checkout.is_symlink() or not checkout.is_dir():
            raise ValueError("Checkout missing or symbolic")
        checkout = checkout.resolve(strict=True)
        top = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != checkout:
            raise ValueError("Checkout path is not a Git root")
        item["git_head"] = _git_oid(_git(checkout, "rev-parse", "HEAD"))
        item["head_relationship"] = _head_relationship(
            checkout, item["git_head"], main_head
        )
        if _git(checkout, "ls-files", "--error-unmatch", "--", _HANDOFF) != _HANDOFF:
            raise ValueError("Handoff is not a tracked file")
        committed = _git_oid(_git(checkout, "rev-parse", f"HEAD:{_HANDOFF}"))
        working_blob = _git_oid(
            _git(checkout, "hash-object", f"--path={_HANDOFF}", "--", _HANDOFF)
        )
        item["committed_blob"] = committed
        working = _handoff_digest(checkout)
        item["working_sha256"] = working
        status = _git(
            checkout, "status", "--porcelain=v1", "--untracked-files=normal",
            "--", _HANDOFF,
        )
        item["git_status"] = "MODIFIED" if status else "CLEAN"
        if working_blob != committed:
            item["classification"] = (
                "UNCOMMITTED_HANDOFF_EDIT"
                if status else "GIT_STATUS_MASKED_HANDOFF_EDIT"
            )
        elif committed != main_blob:
            item["classification"] = (
                "HISTORICAL_COMMITTED_VARIANT" if working != main_sha
                else "DIFFERENT_COMMITTED_BLOB_SAME_BYTES"
            )
        elif working != main_sha:
            # Same tracked Git blob, but different on-disk bytes (for
            # example LF vs CRLF). Not a historical semantic revision.
            item["classification"] = "WORKTREE_BYTE_ENCODING_VARIANT"
        elif status:
            item["classification"] = "HANDOFF_STATUS_CHANGED"
        else:
            item["classification"] = "IDENTICAL_MAIN_BYTES"
    except (OSError, ValueError):
        # Never emit an error string: filesystem and Git errors can contain
        # names of private folders or paths.
        pass
    return item


def reconcile_checkouts(
    root: Path, independent_checkouts: Sequence[Path] | None = None
) -> dict:
    """Generate an ordinal-only inventory; no writes and no cutover decisions."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Root checkout missing or symbolic")
    root = root.resolve(strict=True)
    independent = (
        tuple(independent_checkouts) if independent_checkouts is not None else None
    )
    if independent is not None and len(independent) > _MAX_INDEPENDENT:
        raise ValueError("Independent checkout inventory exceeds bounded limit")
    registered = _worktree_paths(root)
    # The registered list comes from Git; independently supplied clones are
    # separate and must not be represented twice.
    chosen: list[tuple[Path, str, int]] = []
    identities: set[str] = set()
    for kind, entries in (
        ("registered", registered),
        ("independent", independent or ()),
    ):
        for index, location in enumerate(entries, 1):
            path = Path(location)
            try:
                key = str(path.resolve(strict=True)).casefold()
            except OSError:
                key = str(path.absolute()).casefold()
            if key in identities:
                raise ValueError("Duplicate registered or independent checkout")
            identities.add(key)
            chosen.append((path, kind, index))

    pre = scan_handoff_worktrees(root, independent)
    main_sha = pre["source_sha256"]
    if main_sha is None or pre["git_head"] is None:
        raise ValueError("Cannot verify pinned main source identity")

    main_blob = _git_oid(_git(root, "rev-parse", f"HEAD:{_HANDOFF}"))
    main_head = _git_oid(pre["git_head"])
    first = [
        _inspect(p, kind, number, main_sha, main_blob, main_head)
        for p, kind, number in chosen
    ]
    # Rescan both the ownership summary and exact per-checkout fingerprints.
    # A changed version that remains in the same summary category must
    # still invalidate the report's apparently stable inventory.
    post = scan_handoff_worktrees(root, independent)
    second = [
        _inspect(p, kind, number, main_sha, main_blob, main_head)
        for p, kind, number in chosen
    ]
    categories = Counter(item["classification"] for item in first)
    relationships = Counter(item["head_relationship"] for item in first)
    observed = set(pre["observations"])
    if pre != post or first != second:
        observed.add("CHECKOUT_STATE_CHANGED_DURING_RECONCILIATION")
    if any(item["classification"] != "IDENTICAL_MAIN_BYTES" for item in first):
        observed.add("CHECKOUT_VERSIONS_REQUIRE_OWNER_RECONCILIATION")
    if any(item["classification"] == "UNVERIFIABLE" for item in first):
        observed.add("CHECKOUT_PROVENANCE_UNVERIFIABLE")
    if any(item["head_relationship"] in {
        "DESCENDANT_OF_MAIN", "DIVERGED_FROM_MAIN", "UNVERIFIABLE"
    } for item in first):
        observed.add("UNMERGED_OR_UNVERIFIABLE_HEADS_REQUIRE_OWNER_REVIEW")

    return {
        "result": "B3_CHECKOUT_RECONCILIATION_ONLY",
        "source_git_head": pre["git_head"],
        "source_sha256": main_sha,
        "registered_checkout_count": len(registered),
        "independent_checkout_count": (
            None if independent is None else len(independent)
        ),
        "independent_coverage": pre["independent_checkout_coverage"],
        "classification_counts": dict(sorted(categories.items())),
        "head_relationship_counts": dict(sorted(relationships.items())),
        "checkouts": first,
        "blockers": sorted(observed),
        "owner_decisions_verified": False,
        "exclusive_writer_authority": "NOT_ESTABLISHED",
        "historical_semantic_authority": "NOT_VERIFIED",
        "canonical_cutover": "NOT_AUTHORIZED_OR_PERFORMED",
        "can_cutover": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only B3 per-checkout handoff provenance inventory"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--independent-checkout", type=Path, action="append")
    group.add_argument("--declare-no-independent-checkouts", action="store_true")
    args = parser.parse_args(argv)
    report = reconcile_checkouts(
        args.root,
        [] if args.declare_no_independent_checkouts else args.independent_checkout,
    )
    print(json.dumps(report, sort_keys=True))
    # No successful inventory proves authority. Exit code 2 signals unresolved
    # reconciliation or unenumerated clones, never a live cutover.
    return 2 if report["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
