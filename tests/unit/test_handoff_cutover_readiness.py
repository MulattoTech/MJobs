"""Synthetic-only, read-only B3 cutover readiness and ownership tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from tools.verification import handoff_archive
from tools.verification import handoff_private_preview as private
from tools.verification import handoff_cutover_readiness as readiness


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, text=True,
        capture_output=True, timeout=15,
    )
    return result.stdout.strip()


@pytest.fixture
def sample(tmp_path):
    repo = tmp_path / "synthetic-checkout"
    (repo / "planning").mkdir(parents=True)
    original = (
        b"\xef\xbb\xbfHISTORICAL_ONLY_SECRET_TEXT\r\n"
        b"## Previous synthetic note\r\n"
        b"Prior event is NOT current acceptance\r\n"
        b"## Current synthetic note\n"
        b"A current status must be reviewed\n"
    )
    source = repo / "planning" / "SESSION_HANDOFF.md"
    source.write_bytes(original)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "synthetic@example.invalid")
    git(repo, "config", "user.name", "Synthetic Test")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthetic baseline")
    head = git(repo, "rev-parse", "HEAD")
    source_sha = hashlib.sha256(original).hexdigest()

    archive_root = tmp_path / "private-archive"
    archive_root.mkdir(mode=0o700)
    handoff_archive.create_archive(source, archive_root, source_sha)
    snapshot = archive_root / ("sha256-" + source_sha)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    review_root = tmp_path / "private-review"
    review_root.mkdir(mode=0o700)
    review_file = review_root / "synthetic-review.json"
    review_file.write_text(json.dumps({
        "schema_version": 1,
        "source_sha256": source_sha,
        "reviewed_by": "Synthetic-only reviewer",
        "reviewed_at_utc": "2026-11-20T20:00:00Z",
        "review_reference": "synthetic-only-review",
        "entries": [{
            "subject": "Current synthetic status",
            "checkpoint_id": manifest["checkpoints"][-1]["checkpoint_id"],
            "status": "UNKNOWN",
            "summary": "Synthetic reviewed current status",
            "evidence": "Synthetic-only fixture evidence",
            "unknowns": "No verified real acceptance or deployment",
        }],
    }), encoding="utf-8")
    review_sha = hashlib.sha256(review_file.read_bytes()).hexdigest()
    preview_root = tmp_path / "private-previews"
    preview_root.mkdir(mode=0o700)
    created = private.create_candidate(
        source, snapshot, review_file, source_sha, preview_root
    )
    candidate = preview_root / ("review-preview-sha256-" + created["preview_sha256"])
    return {
        "root": tmp_path, "repo": repo, "source": source,
        "original": original, "head": head,
        "source_sha": source_sha, "snapshot": snapshot,
        "review_file": review_file, "review_sha": review_sha,
        "candidate": candidate, "preview_sha": created["preview_sha256"],
        "preview_root": preview_root, "archive_root": archive_root,
    }


def pins(s):
    return {
        "independent_checkouts": [],
        "expected_head": s["head"],
        "expected_source_sha256": s["source_sha"],
        "snapshot": s["snapshot"],
        "review_file": s["review_file"],
        "expected_review_sha256": s["review_sha"],
        "candidate": s["candidate"],
        "expected_preview_sha256": s["preview_sha"],
    }


def _fingerprint(s):
    return {
        "original": s["source"].read_bytes(),
        "review": s["review_file"].read_bytes(),
        "snapshot_manifest": (s["snapshot"] / "manifest.json").read_bytes(),
        "candidate_receipt": (s["candidate"] / "receipt.json").read_bytes(),
        "candidate_preview": (s["candidate"] / "preview.md").read_bytes(),
        "tracked_status": git(s["repo"], "status", "--porcelain"),
    }


def test_scan_only_reports_no_authority_and_no_private_history(sample):
    before = _fingerprint(sample)
    report = readiness.readiness_report(sample["repo"])
    assert report["result"] == "B3_CUTOVER_READINESS_ONLY"
    assert report["mode"] == "WORKTREE_SCAN"
    assert report["source_git_head"] == sample["head"]
    assert report["source_git_branch"] == "main"
    assert report["source_sha256"] == sample["source_sha"]
    assert report["source_length"] == len(sample["original"])
    assert report["worktree_count"] == 1
    assert report["dirty_handoff_worktrees"] == 0
    assert report["unverifiable_worktrees"] == 0
    assert report["technical_evidence"] == "NOT_QUERIED"
    assert "PRIVATE_EVIDENCE_NOT_SUPPLIED" in report["blockers"]
    assert "INDEPENDENT_CHECKOUTS_NOT_ENUMERATED" in report["blockers"]
    assert report["independent_checkout_coverage"] == "NOT_DECLARED"
    assert report["can_cutover"] is False
    assert report["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert report["remote_protection"] == "NOT_QUERIED"
    assert "HISTORICAL_ONLY_SECRET_TEXT" not in json.dumps(report)
    assert "Synthetic reviewed current status" not in json.dumps(report)
    assert before == _fingerprint(sample)


def test_full_evidence_can_be_consistent_but_cannot_authorize_cutover(sample):
    before = _fingerprint(sample)
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert report["mode"] == "FULL_EVIDENCE"
    assert report["independent_checkout_coverage"] == "CALLER_DECLARED_NOT_AUTHENTICATED"
    assert report["independent_checkout_count"] == 0
    assert report["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
    assert report["blockers"] == []
    assert report["ready_for_review"] is True
    assert report["preview_sha256"] == sample["preview_sha"]
    assert report["review_sha256"] == sample["review_sha"]
    assert report["legacy_rollback_sha256"] == sample["source_sha"]
    assert report["can_cutover"] is False
    assert report["review_authenticity"] == "NOT_VERIFIED"
    assert report["semantic_current_state"] == "NOT_VERIFIED"
    assert report["single_writer_authority"] == "NOT_ESTABLISHED"
    assert report["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert report["remote_protection"] == "NOT_QUERIED"
    assert before == _fingerprint(sample)


@pytest.mark.parametrize("changed", [
    ("expected_head", "0" * 40),
    ("expected_source_sha256", "0" * 64),
    ("expected_review_sha256", "0" * 64),
    ("expected_preview_sha256", "0" * 64),
    ("expected_source_sha256", "not-a-hash"),
])
def test_wrong_or_invalid_pins_fail_closed(sample, changed):
    inputs = pins(sample)
    inputs[changed[0]] = changed[1]
    report = readiness.readiness_report(sample["repo"], **inputs)
    assert report["ready_for_review"] is False
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]
    assert report["can_cutover"] is False


def test_partial_private_evidence_does_not_quietly_pass(sample):
    report = readiness.readiness_report(
        sample["repo"], expected_head=sample["head"],
        expected_source_sha256=sample["source_sha"],
    )
    assert report["technical_evidence"] == "NOT_QUERIED"
    assert report["ready_for_review"] is False
    assert "INCOMPLETE_PINNED_PRIVATE_EVIDENCE" in report["blockers"]


def test_modified_main_source_is_a_blocker(sample):
    source = sample["source"]
    source.write_bytes(source.read_bytes() + b"SIMULATED_EXTERNAL_EDIT")
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert "UNCOMMITTED_HANDOFF_IN_WORKTREE" in report["blockers"]
    assert report["dirty_handoff_worktrees"] == 1
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False


def test_another_registered_worktree_with_uncommitted_handoff_blocks(sample):
    repo = sample["repo"]
    sibling = sample["root"] / "other-agent-checkout"
    git(repo, "worktree", "add", "-q", "-b", "agent-owned", str(sibling))
    source = sibling / "planning" / "SESSION_HANDOFF.md"
    source.write_bytes(source.read_bytes() + b"SIMULATED_OTHER_OWNER_EDIT")
    before = sample["source"].read_bytes()
    report = readiness.readiness_report(repo, **pins(sample))
    assert report["worktree_count"] == 2
    assert report["dirty_handoff_worktrees"] == 1
    assert report["unverifiable_worktrees"] == 0
    assert "UNCOMMITTED_HANDOFF_IN_WORKTREE" in report["blockers"]
    assert report["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False
    assert sample["source"].read_bytes() == before
    assert str(sibling) not in json.dumps(report)


def test_non_main_source_checkout_is_not_treated_as_cutover_authority(sample):
    git(sample["repo"], "switch", "-q", "-c", "other-branch")
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert "SOURCE_CHECKOUT_NOT_ON_MAIN" in report["blockers"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False


def test_changed_review_selection_cannot_be_accepted_on_old_review_pin(sample):
    sample["review_file"].write_bytes(sample["review_file"].read_bytes() + b" ")
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]


def test_candidate_receipt_tamper_fails_even_if_preview_bytes_unchanged(sample):
    receipt = sample["candidate"] / "receipt.json"
    modified = json.loads(receipt.read_text(encoding="utf-8"))
    modified["review_sha256"] = "0" * 64
    receipt.write_text(json.dumps(modified), encoding="utf-8")
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]


@pytest.mark.parametrize("lock", ["archive", "preview"])
def test_unowned_writer_lock_blocks_even_read_only_full_preflight(sample, lock):
    if lock == "archive":
        path = sample["archive_root"] / handoff_archive._LOCK
    else:
        path = sample["preview_root"] / private._LOCK_NAME
    path.write_bytes(b"ANOTHER_SYNTHETIC_WRITER")
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert report["ready_for_review"] is False
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]
    assert path.read_bytes() == b"ANOTHER_SYNTHETIC_WRITER"


def test_staged_private_preview_blocks_full_preflight(sample):
    stage = sample["preview_root"] / ".preview-stage-synthetic-abandoned"
    stage.mkdir()
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert report["ready_for_review"] is False
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]
    assert stage.exists()


def test_private_reviewer_file_inside_git_repo_refused(sample):
    reviewer = sample["repo"] / "review.json"
    reviewer.write_bytes(sample["review_file"].read_bytes())
    values = pins(sample)
    values["review_file"] = reviewer
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]


def test_cli_scan_exit_code_is_two_and_does_not_print_private_text(sample, capsys):
    rc = readiness.main(["--root", str(sample["repo"])])
    output = capsys.readouterr().out
    report = json.loads(output)
    assert rc == 2
    assert report["ready_for_review"] is False
    assert "HISTORICAL_ONLY_SECRET_TEXT" not in output
    assert "Synthetic reviewed current status" not in output


def test_cli_complete_synthetic_evidence_is_review_only_not_cutover(sample, capsys):
    opts = [
        "--root", str(sample["repo"]),
        "--attest-no-independent-checkouts",
        "--expected-head", sample["head"],
        "--expected-source-sha256", sample["source_sha"],
        "--snapshot", str(sample["snapshot"]),
        "--review-file", str(sample["review_file"]),
        "--expected-review-sha256", sample["review_sha"],
        "--candidate", str(sample["candidate"]),
        "--expected-preview-sha256", sample["preview_sha"],
    ]
    assert readiness.main(opts) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ready_for_review"] is True
    assert report["can_cutover"] is False
    assert "HISTORICAL_ONLY_SECRET_TEXT" not in output
    assert "Synthetic reviewed current status" not in output



def test_technical_evidence_without_independent_checkout_inventory_is_blocked(sample):
    values = pins(sample)
    del values["independent_checkouts"]
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
    assert report["independent_checkout_coverage"] == "NOT_DECLARED"
    assert "INDEPENDENT_CHECKOUTS_NOT_ENUMERATED" in report["blockers"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False


def test_separate_git_clone_with_dirty_handoff_blocks_even_when_archive_valid(sample):
    clone = sample["root"] / "independent-clone"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    independent_handoff = clone / "planning" / "SESSION_HANDOFF.md"
    independent_handoff.write_bytes(
        independent_handoff.read_bytes() + b"INDEPENDENT_AGENT_UNCOMMITTED_UPDATE"
    )
    original = sample["source"].read_bytes()
    values = pins(sample)
    values["independent_checkouts"] = [clone]
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["worktree_count"] == 1
    assert report["independent_checkout_count"] == 1
    assert report["dirty_independent_checkouts"] == 1
    assert report["unverifiable_independent_checkouts"] == 0
    assert "UNCOMMITTED_HANDOFF_IN_INDEPENDENT_CHECKOUT" in report["blockers"]
    assert report["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False
    assert sample["source"].read_bytes() == original
    assert str(clone) not in json.dumps(report)
    assert "INDEPENDENT_AGENT_UNCOMMITTED_UPDATE" not in json.dumps(report)


def test_unknown_independent_checkout_cannot_be_counted_clean(sample):
    values = pins(sample)
    values["independent_checkouts"] = [sample["root"] / "nonexistent-clone"]
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["unverifiable_independent_checkouts"] == 1
    assert "INDEPENDENT_CHECKOUT_STATUS_UNVERIFIABLE" in report["blockers"]
    assert report["ready_for_review"] is False


def test_registered_worktree_cannot_be_double_counted_as_independent(sample):
    values = pins(sample)
    values["independent_checkouts"] = [sample["repo"]]
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["unverifiable_independent_checkouts"] == 1
    assert "INDEPENDENT_CHECKOUT_STATUS_UNVERIFIABLE" in report["blockers"]
    assert report["can_cutover"] is False


def test_cli_scan_with_explicit_other_clone_reports_only_counts(sample, capsys):
    clone = sample["root"] / "independent-clone"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    (clone / "planning" / "SESSION_HANDOFF.md").write_bytes(
        sample["original"] + b"SYNTHETIC_DIRTY_OTHER_CLONE"
    )
    assert readiness.main([
        "--root", str(sample["repo"]),
        "--independent-checkout", str(clone),
    ]) == 2
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["dirty_independent_checkouts"] == 1
    assert result["independent_checkout_count"] == 1
    assert result["independent_checkout_coverage"] == "CALLER_DECLARED_NOT_AUTHENTICATED"
    assert "SYNTHETIC_DIRTY_OTHER_CLONE" not in output
    assert str(clone) not in output


def test_cli_must_not_mix_attestation_and_explicit_other_checkout(sample):
    with pytest.raises(SystemExit) as error:
        readiness.main([
            "--root", str(sample["repo"]),
            "--independent-checkout", str(sample["repo"]),
            "--attest-no-independent-checkouts",
        ])
    assert error.value.code == 2



def test_full_preflight_rejects_candidate_nested_in_immutable_archive(sample):
    import shutil

    inside = sample["snapshot"] / sample["candidate"].name
    shutil.copytree(sample["candidate"], inside)
    values = pins(sample)
    values["candidate"] = inside
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]
    assert report["can_cutover"] is False


def test_full_preflight_rejects_review_file_nested_inside_archive(sample):
    inside = sample["snapshot"] / "unreviewed-copy.json"
    inside.write_bytes(sample["review_file"].read_bytes())
    values = pins(sample)
    values["review_file"] = inside
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_UNVERIFIABLE" in report["blockers"]
    assert report["can_cutover"] is False


def test_source_mutation_during_evidence_verification_rejected_at_postflight(
    sample, monkeypatch
):
    verified = readiness.verify_candidate

    def mutate_source_after_candidate_check(*args, **kwargs):
        result = verified(*args, **kwargs)
        sample["source"].write_bytes(
            sample["original"] + b"SIMULATED_MID_AUDIT_WRITER_UPDATE"
        )
        return result

    monkeypatch.setattr(
        readiness, "verify_candidate", mutate_source_after_candidate_check
    )
    result = readiness.readiness_report(sample["repo"], **pins(sample))
    assert result["technical_evidence"] == "FAILED"
    assert "HANDOFF_OR_CHECKOUT_STATE_CHANGED_DURING_VERIFICATION" in result["blockers"]
    assert "UNCOMMITTED_HANDOFF_IN_WORKTREE" in result["blockers"]
    assert result["ready_for_review"] is False
    assert result["can_cutover"] is False


def test_independent_clone_mutation_during_audit_rejected_at_postflight(
    sample, monkeypatch
):
    clone = sample["root"] / "independent-postflight-clone"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    values = pins(sample)
    values["independent_checkouts"] = [clone]
    verified = readiness.verify_candidate

    def mutate_independent_after_candidate_check(*args, **kwargs):
        result = verified(*args, **kwargs)
        file = clone / "planning" / "SESSION_HANDOFF.md"
        file.write_bytes(file.read_bytes() + b"OTHER_AGENT_CHANGED_DURING_AUDIT")
        return result

    monkeypatch.setattr(
        readiness, "verify_candidate", mutate_independent_after_candidate_check
    )
    report = readiness.readiness_report(sample["repo"], **values)
    assert report["technical_evidence"] == "FAILED"
    assert "HANDOFF_OR_CHECKOUT_STATE_CHANGED_DURING_VERIFICATION" in report["blockers"]
    assert "UNCOMMITTED_HANDOFF_IN_INDEPENDENT_CHECKOUT" in report["blockers"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False
    assert str(clone) not in json.dumps(report)


def test_git_head_changing_after_evidence_validation_is_rejected(
    sample, monkeypatch
):
    verified = readiness.verify_candidate

    def advance_synthetic_head(*args, **kwargs):
        result = verified(*args, **kwargs)
        new_file = sample["repo"] / "synthetic-revision-note.txt"
        new_file.write_text("A parallel agent committed another revision")
        git(sample["repo"], "add", "synthetic-revision-note.txt")
        git(sample["repo"], "commit", "-qm", "parallel synthetic commit")
        return result

    monkeypatch.setattr(readiness, "verify_candidate", advance_synthetic_head)
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert report["technical_evidence"] == "FAILED"
    assert "HANDOFF_OR_CHECKOUT_STATE_CHANGED_DURING_VERIFICATION" in report["blockers"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False


def test_review_selection_changed_after_initial_verification_fails_closed(
    sample, monkeypatch
):
    first_verify = readiness.verify_candidate
    called = [0]

    def mutate_review_after_first_check(*args, **kwargs):
        result = first_verify(*args, **kwargs)
        called[0] += 1
        if called[0] == 1:
            sample["review_file"].write_bytes(
                sample["review_file"].read_bytes() + b" "
            )
        return result

    monkeypatch.setattr(
        readiness, "verify_candidate", mutate_review_after_first_check
    )
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert called[0] == 1
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_CHANGED_DURING_VERIFICATION" in report["blockers"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False


def test_archived_checkpoint_changed_after_initial_validation_fails_closed(
    sample, monkeypatch
):
    first_verify = readiness.verify_candidate
    calls = [0]
    piece = sorted((sample["snapshot"] / "pieces").glob("*.bin"))[0]

    def corrupt_archive_after_first_candidate_check(*args, **kwargs):
        result = first_verify(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 1:
            piece.write_bytes(piece.read_bytes() + b"SYNTHETIC_ARCHIVE_TAMPER")
        return result

    monkeypatch.setattr(
        readiness, "verify_candidate", corrupt_archive_after_first_candidate_check
    )
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert calls[0] == 1
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_CHANGED_DURING_VERIFICATION" in report["blockers"]
    assert report["ready_for_review"] is False


def test_candidate_changed_after_initial_validation_fails_closed(
    sample, monkeypatch
):
    first_verify = readiness.verify_candidate
    calls = [0]

    def corrupt_candidate_after_first_check(*args, **kwargs):
        # Count attempted verifications, including one correctly rejected
        # before verify_candidate can return an accepted result.
        calls[0] += 1
        result = first_verify(*args, **kwargs)
        if calls[0] == 1:
            file = sample["candidate"] / "preview.md"
            file.write_bytes(file.read_bytes() + b"UNREVIEWED_EXTRA_TEXT")
        return result

    monkeypatch.setattr(readiness, "verify_candidate", corrupt_candidate_after_first_check)
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert calls[0] == 2
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_CHANGED_DURING_VERIFICATION" in report["blockers"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False


def test_new_writer_lock_after_initial_verification_fails_closed(
    sample, monkeypatch
):
    first_verify = readiness.verify_candidate
    calls = [0]
    lock = sample["preview_root"] / private._LOCK_NAME

    def introduce_foreign_lock(*args, **kwargs):
        result = first_verify(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 1:
            lock.write_bytes(b"FOREIGN_WRITER_STARTED")
        return result

    monkeypatch.setattr(readiness, "verify_candidate", introduce_foreign_lock)
    report = readiness.readiness_report(sample["repo"], **pins(sample))
    assert calls[0] == 1
    assert report["technical_evidence"] == "FAILED"
    assert "PINNED_PRIVATE_EVIDENCE_CHANGED_DURING_VERIFICATION" in report["blockers"]
    assert report["ready_for_review"] is False
    assert lock.read_bytes() == b"FOREIGN_WRITER_STARTED"



def test_registered_clean_branch_with_different_committed_handoff_blocks(sample):
    """A committed older/newer handoff is not an uncommitted edit, but blocks."""
    repo = sample["repo"]
    sibling = sample["root"] / "independent-agent-worktree"
    git(repo, "worktree", "add", "-q", "-b", "synthetic-other-branch", str(sibling))
    other_source = sibling / "planning" / "SESSION_HANDOFF.md"
    other_source.write_bytes(other_source.read_bytes() + b"OTHER_BRANCH_HANDOFF_COMMIT")
    git(sibling, "add", "planning/SESSION_HANDOFF.md")
    git(sibling, "commit", "-qm", "synthetic other handoff")
    assert git(sibling, "status", "--porcelain") == ""

    report = readiness.readiness_report(repo, **pins(sample))
    assert report["worktree_count"] == 2
    assert report["dirty_handoff_worktrees"] == 0
    assert report["different_handoff_worktrees"] == 1
    assert "HANDOFF_BYTES_DIFFER_IN_WORKTREE" in report["blockers"]
    assert report["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False
    assert str(sibling) not in json.dumps(report)


def test_independent_assume_unchanged_handoff_is_detected_by_digest(sample):
    """Git status may hide a modified tracked file; compare raw bytes anyway."""
    clone = sample["root"] / "assume-unchanged-synthetic-clone"
    git(sample["repo"], "clone", "-q", str(sample["repo"]), str(clone))
    git(clone, "update-index", "--assume-unchanged", "planning/SESSION_HANDOFF.md")
    changed = clone / "planning" / "SESSION_HANDOFF.md"
    changed.write_bytes(changed.read_bytes() + b"HIDDEN_INDEPENDENT_HANDOFF_CHANGE")
    assert git(clone, "status", "--porcelain", "--", "planning/SESSION_HANDOFF.md") == ""

    values = pins(sample)
    values["independent_checkouts"] = [clone]
    result = readiness.readiness_report(sample["repo"], **values)
    assert result["dirty_independent_checkouts"] == 0
    assert result["different_independent_checkouts"] == 1
    assert "HANDOFF_BYTES_DIFFER_IN_INDEPENDENT_CHECKOUT" in result["blockers"]
    assert result["technical_evidence"] == "SOURCE_ARCHIVE_REVIEW_PREVIEW_CONSISTENT"
    assert result["ready_for_review"] is False
    assert result["can_cutover"] is False
    assert "HIDDEN_INDEPENDENT_HANDOFF_CHANGE" not in json.dumps(result)


def test_canonical_assume_unchanged_handoff_differs_from_committed_head(sample):
    """Root handoff cannot be trusted from Git status even with a matching pin."""
    repo = sample["repo"]
    git(repo, "update-index", "--assume-unchanged", "planning/SESSION_HANDOFF.md")
    source = sample["source"]
    source.write_bytes(source.read_bytes() + b"HIDDEN_ROOT_HANDOFF_CHANGE")
    assert git(repo, "status", "--porcelain", "--", "planning/SESSION_HANDOFF.md") == ""

    report = readiness.readiness_report(repo)
    assert "SOURCE_HANDOFF_DIFFERS_FROM_COMMITTED_HEAD" in report["blockers"]
    assert report["source_git_head"] == sample["head"]
    assert report["source_sha256"] != sample["source_sha"]
    assert report["ready_for_review"] is False
    assert report["can_cutover"] is False
    assert "HIDDEN_ROOT_HANDOFF_CHANGE" not in json.dumps(report)


def test_registered_assume_unchanged_worktree_is_detected_by_digest(sample):
    repo = sample["repo"]
    sibling = sample["root"] / "hidden-modified-worktree"
    git(repo, "worktree", "add", "-q", "-b", "hidden-changes", str(sibling))
    git(sibling, "update-index", "--assume-unchanged", "planning/SESSION_HANDOFF.md")
    source = sibling / "planning" / "SESSION_HANDOFF.md"
    source.write_bytes(source.read_bytes() + b"ASSUME_UNCHANGED_REVIEW_TEST")
    assert git(sibling, "status", "--porcelain", "--", "planning/SESSION_HANDOFF.md") == ""

    result = readiness.readiness_report(repo, **pins(sample))
    assert result["dirty_handoff_worktrees"] == 0
    assert result["different_handoff_worktrees"] == 1
    assert "HANDOFF_BYTES_DIFFER_IN_WORKTREE" in result["blockers"]
    assert result["ready_for_review"] is False
    assert result["can_cutover"] is False
