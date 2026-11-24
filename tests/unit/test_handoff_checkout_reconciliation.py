"""Synthetic-only handoff checkout reconciliation metadata tests."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from tools.verification import handoff_checkout_reconciliation as inventory


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        text=True, capture_output=True, timeout=15,
    )
    return p.stdout.strip()


@pytest.fixture
def sample(tmp_path):
    repo = tmp_path / "synthetic-main"
    (repo / "planning").mkdir(parents=True)
    old = b"## SYNTHETIC_CURRENT_ONLY\nNo real history or acceptance\n"
    path = repo / "planning" / "SESSION_HANDOFF.md"
    path.write_bytes(old)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Synthetic")
    git(repo, "config", "user.email", "synthetic@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthetic baseline")
    return {"root": tmp_path, "repo": repo, "source": path, "original": old}


def other_branch(sample, *, suffix=b"NEW_SYNTHETIC_VERSION"):
    checkout = sample["root"] / ("independent-worktree-" + str(len(list(sample["root"].iterdir()))))
    git(
        sample["repo"], "worktree", "add", "-q", "-b",
        "older-synthetic-branch-" + checkout.name, str(checkout),
    )
    file = checkout / "planning" / "SESSION_HANDOFF.md"
    file.write_bytes(file.read_bytes() + b"\n" + suffix + b"\n")
    git(checkout, "add", "planning/SESSION_HANDOFF.md")
    git(checkout, "commit", "-qm", "alternate synthetic checkpoint")
    return checkout


def test_single_clean_checkout_no_other_clones_declared_still_not_cutover(sample):
    before = sample["source"].read_bytes()
    report = inventory.reconcile_checkouts(sample["repo"], [])
    assert report["result"] == "B3_CHECKOUT_RECONCILIATION_ONLY"
    assert report["registered_checkout_count"] == 1
    assert report["independent_checkout_count"] == 0
    assert report["classification_counts"] == {"IDENTICAL_MAIN_BYTES": 1}
    assert report["head_relationship_counts"] == {"EXACT_MAIN": 1}
    assert report["checkouts"][0]["classification"] == "IDENTICAL_MAIN_BYTES"
    assert report["checkouts"][0]["checkout_ref"] == "registered-01"
    assert report["blockers"] == []
    assert report["can_cutover"] is False
    assert report["exclusive_writer_authority"] == "NOT_ESTABLISHED"
    assert report["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert sample["source"].read_bytes() == before
    assert "SYNTHETIC_CURRENT_ONLY" not in json.dumps(report)
    assert str(sample["repo"]) not in json.dumps(report)


def test_unknown_independent_clones_are_not_assumed_absent(sample):
    result = inventory.reconcile_checkouts(sample["repo"])
    assert "INDEPENDENT_CHECKOUTS_NOT_ENUMERATED" in result["blockers"]
    assert result["independent_checkout_count"] is None
    assert result["can_cutover"] is False


def test_registered_other_committed_branch_is_historical_version(sample):
    different = other_branch(sample)
    assert git(different, "status", "--porcelain") == ""
    result = inventory.reconcile_checkouts(sample["repo"], [])
    assert result["registered_checkout_count"] == 2
    assert result["classification_counts"] == {
        "IDENTICAL_MAIN_BYTES": 1,
        "HISTORICAL_COMMITTED_VARIANT": 1,
    }
    rows = result["checkouts"]
    assert rows[1]["classification"] == "HISTORICAL_COMMITTED_VARIANT"
    assert rows[1]["head_relationship"] == "DESCENDANT_OF_MAIN"
    assert "UNMERGED_OR_UNVERIFIABLE_HEADS_REQUIRE_OWNER_REVIEW" in result["blockers"]
    assert rows[1]["git_status"] == "CLEAN"
    assert "CHECKOUT_VERSIONS_REQUIRE_OWNER_RECONCILIATION" in result["blockers"]
    assert str(different) not in json.dumps(result)


def test_independent_clean_clone_with_same_bytes(sample):
    clone = sample["root"] / "synthetic-independent"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    result = inventory.reconcile_checkouts(sample["repo"], [clone])
    assert result["registered_checkout_count"] == 1
    assert result["independent_checkout_count"] == 1
    # Windows and Git core.autocrlf can produce different raw checkout
    # bytes for the same committed Git blob without any local edit.
    class_name = result["checkouts"][1]["classification"]
    main_bytes = sample["source"].read_bytes()
    clone_bytes = (clone / "planning" / "SESSION_HANDOFF.md").read_bytes()
    assert class_name == (
        "IDENTICAL_MAIN_BYTES" if main_bytes == clone_bytes
        else "WORKTREE_BYTE_ENCODING_VARIANT"
    )
    assert result["checkouts"][1]["committed_blob"] == (
        result["checkouts"][0]["committed_blob"]
    )
    assert result["head_relationship_counts"] == {"EXACT_MAIN": 2}
    assert result["checkouts"][1]["checkout_ref"] == "independent-01"
    assert result["checkouts"][1]["kind"] == "independent"
    expected_blockers = set() if main_bytes == clone_bytes else {
        "CHECKOUT_VERSIONS_REQUIRE_OWNER_RECONCILIATION",
        "HANDOFF_BYTES_DIFFER_IN_INDEPENDENT_CHECKOUT",
    }
    assert set(result["blockers"]) == expected_blockers


def test_independent_local_edits_distinguished_from_committed_history(sample):
    clone = sample["root"] / "synthetic-dirty-clone"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    handoff = clone / "planning" / "SESSION_HANDOFF.md"
    handoff.write_bytes(handoff.read_bytes() + b"PRIVATE_LOCAL_CHANGE")
    result = inventory.reconcile_checkouts(sample["repo"], [clone])
    assert result["classification_counts"]["UNCOMMITTED_HANDOFF_EDIT"] == 1
    assert result["checkouts"][1]["git_status"] == "MODIFIED"
    assert "PRIVATE_LOCAL_CHANGE" not in json.dumps(result)
    assert "CHECKOUT_VERSIONS_REQUIRE_OWNER_RECONCILIATION" in result["blockers"]


def test_assume_unchanged_flag_does_not_hide_independent_edit(sample):
    clone = sample["root"] / "synthetic-hidden-clone"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    git(clone, "update-index", "--assume-unchanged", "planning/SESSION_HANDOFF.md")
    handoff = clone / "planning" / "SESSION_HANDOFF.md"
    handoff.write_bytes(handoff.read_bytes() + b"ASSUME_UNCHANGED_PRIVACY_TEST")
    assert git(clone, "status", "--porcelain", "--", "planning/SESSION_HANDOFF.md") == ""
    result = inventory.reconcile_checkouts(sample["repo"], [clone])
    assert result["classification_counts"]["GIT_STATUS_MASKED_HANDOFF_EDIT"] == 1
    assert result["checkouts"][1]["git_status"] == "CLEAN"
    assert "ASSUME_UNCHANGED_PRIVACY_TEST" not in json.dumps(result)


def test_missing_independent_checkout_is_unverifiable_not_clean(sample):
    result = inventory.reconcile_checkouts(
        sample["repo"], [sample["root"] / "not-a-repo"]
    )
    assert result["checkouts"][1]["classification"] == "UNVERIFIABLE"
    assert result["classification_counts"]["UNVERIFIABLE"] == 1
    assert "CHECKOUT_PROVENANCE_UNVERIFIABLE" in result["blockers"]


def test_duplicate_independent_and_registered_checkout_rejected(sample):
    with pytest.raises(ValueError, match="Duplicate"):
        inventory.reconcile_checkouts(sample["repo"], [sample["repo"]])


def test_large_independent_list_rejected(sample):
    many = [sample["root"] / str(i) for i in range(33)]
    with pytest.raises(ValueError, match="bounded"):
        inventory.reconcile_checkouts(sample["repo"], many)


def test_root_hidden_uncommitted_edit_causes_main_fingerprint_block(sample):
    git(sample["repo"], "update-index", "--assume-unchanged", "planning/SESSION_HANDOFF.md")
    sample["source"].write_bytes(sample["original"] + b"UNREVIEWED_MAIN_CHANGE")
    report = inventory.reconcile_checkouts(sample["repo"], [])
    assert "SOURCE_HANDOFF_DIFFERS_FROM_COMMITTED_HEAD" in report["blockers"]
    assert report["classification_counts"]["GIT_STATUS_MASKED_HANDOFF_EDIT"] == 1
    assert "UNREVIEWED_MAIN_CHANGE" not in json.dumps(report)


def test_mid_scan_bytes_mutation_prevents_stable_inventory(sample, monkeypatch):
    original = inventory._inspect
    called = [0]

    def mutate_after_first_inspect(*args, **kwargs):
        result = original(*args, **kwargs)
        called[0] += 1
        if called[0] == 1:
            sample["source"].write_bytes(
                sample["original"] + b"SIMULATED_CONCURRENT_WRITER"
            )
        return result

    monkeypatch.setattr(inventory, "_inspect", mutate_after_first_inspect)
    report = inventory.reconcile_checkouts(sample["repo"], [])
    assert called[0] == 2
    assert "CHECKOUT_STATE_CHANGED_DURING_RECONCILIATION" in report["blockers"]
    assert report["can_cutover"] is False
    assert "SIMULATED_CONCURRENT_WRITER" not in json.dumps(report)


def test_cli_metadata_only_and_no_cutover(sample, capsys):
    assert inventory.main([
        "--root", str(sample["repo"]), "--declare-no-independent-checkouts"
    ]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["can_cutover"] is False
    assert payload["blockers"] == []
    assert "SYNTHETIC_CURRENT_ONLY" not in output
    assert str(sample["root"]) not in output


def test_cli_without_coverage_has_nonzero_exit(sample, capsys):
    assert inventory.main(["--root", str(sample["repo"])]) == 2
    report = json.loads(capsys.readouterr().out)
    assert "INDEPENDENT_CHECKOUTS_NOT_ENUMERATED" in report["blockers"]


def test_cli_rejects_conflicting_coverage_modes(sample):
    with pytest.raises(SystemExit) as result:
        inventory.main([
            "--root", str(sample["repo"]),
            "--independent-checkout", str(sample["repo"]),
            "--declare-no-independent-checkouts",
        ])
    assert result.value.code == 2


def test_source_absent_or_symbolic_checkout_is_unverifiable(sample):
    clone = sample["root"] / "synthetic-other"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    (clone / "planning" / "SESSION_HANDOFF.md").unlink()
    report = inventory.reconcile_checkouts(sample["repo"], [clone])
    assert report["classification_counts"]["UNVERIFIABLE"] == 1
    assert report["can_cutover"] is False



def test_old_registered_branch_is_ancestor_of_new_main_commit(sample):
    repo = sample["repo"]
    prior = sample["root"] / "synthetic-ancestor"
    git(repo, "worktree", "add", "-q", "-b", "synthetic-ancestor-branch", str(prior))
    source = sample["source"]
    source.write_bytes(source.read_bytes() + b"MAIN_ONLY_SYNTHETIC_UPDATE\n")
    git(repo, "add", "planning/SESSION_HANDOFF.md")
    git(repo, "commit", "-qm", "advance main synthetic handoff")

    report = inventory.reconcile_checkouts(repo, [])
    assert report["head_relationship_counts"] == {
        "ANCESTOR_OF_MAIN": 1,
        "EXACT_MAIN": 1,
    }
    assert report["checkouts"][1]["head_relationship"] == "ANCESTOR_OF_MAIN"
    assert report["checkouts"][1]["classification"] == "HISTORICAL_COMMITTED_VARIANT"
    assert report["can_cutover"] is False
    assert str(prior) not in json.dumps(report)


def test_diverged_registered_branch_is_not_misreported_as_ancestor(sample):
    branch = other_branch(sample, suffix=b"UNMERGED_SYNTHETIC_WORK")
    main_source = sample["source"]
    main_source.write_bytes(main_source.read_bytes() + b"DIFFERENT_MAIN_SYNTHETIC_WORK\n")
    git(sample["repo"], "add", "planning/SESSION_HANDOFF.md")
    git(sample["repo"], "commit", "-qm", "different main synthetic commit")
    report = inventory.reconcile_checkouts(sample["repo"], [])
    assert report["head_relationship_counts"] == {
        "DIVERGED_FROM_MAIN": 1,
        "EXACT_MAIN": 1,
    }
    assert report["checkouts"][1]["head_relationship"] == "DIVERGED_FROM_MAIN"
    assert "UNMERGED_OR_UNVERIFIABLE_HEADS_REQUIRE_OWNER_REVIEW" in report["blockers"]
    assert report["can_cutover"] is False
    assert str(branch) not in json.dumps(report)


def test_independent_clone_without_new_main_object_fails_ancestry_closed(sample):
    clone = sample["root"] / "synthetic-old-independent-clone"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    main = sample["source"]
    main.write_bytes(main.read_bytes() + b"NEW_MAIN_OBJECT_NOT_IN_OLD_CLONE\n")
    git(sample["repo"], "add", "planning/SESSION_HANDOFF.md")
    git(sample["repo"], "commit", "-qm", "new main after clone")
    report = inventory.reconcile_checkouts(sample["repo"], [clone])
    assert report["checkouts"][1]["head_relationship"] == "UNVERIFIABLE"
    assert report["head_relationship_counts"]["UNVERIFIABLE"] == 1
    assert "UNMERGED_OR_UNVERIFIABLE_HEADS_REQUIRE_OWNER_REVIEW" in report["blockers"]
    assert report["can_cutover"] is False
    assert str(clone) not in json.dumps(report)
