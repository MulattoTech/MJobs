"""Synthetic-only tests of the in-memory B3 cutover/rollback rehearsal."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path

import pytest

from tools.verification import handoff_archive
from tools.verification import handoff_cutover_rehearsal as cutover


@pytest.fixture
def fixtures(tmp_path):
    source = tmp_path / "private-synthetic-source.md"
    original = (
        b"\xef\xbb\xbfPRIVATE_SYNTHETIC_CHECKPOINT\r\n"
        b"## Historic\r\n"
        b"Previously blocked -- not a PASS assertion\r\n"
        b"## Latest\r\n"
        b"Current human-reviewed fixture state\r\n"
    )
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    root = tmp_path / "archive"
    root.mkdir()
    handoff_archive.create_archive(source, root, digest)
    snapshot = root / f"sha256-{digest}"
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    section_id = manifest["checkpoints"][-1]["checkpoint_id"]
    review = tmp_path / "explicit-review.json"
    review.write_text(json.dumps({
        "schema_version": 1,
        "source_sha256": digest,
        "reviewed_by": "Synthetic Independent Reviewer",
        "reviewed_at_utc": "2026-11-20T18:00:00Z",
        "review_reference": "Synthetic PR review record",
        "entries": [{
            "subject": "B3 rehearsal",
            "checkpoint_id": section_id,
            "status": "UNKNOWN",
            "summary": "Synthetic reviewed current handoff",
            "evidence": "Synthetic-only source proof",
            "unknowns": "No live approval, readiness, or deployment",
        }],
    }), encoding="utf-8")
    return source, snapshot, review, digest, original, tmp_path


def _fingerprints(folder: Path):
    return {
        p.relative_to(folder).as_posix(): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in folder.rglob("*")
        if p.is_file()
    }


def test_full_rehearsal_validates_in_memory_cutover_and_exact_rollback(fixtures):
    source, snapshot, review, digest, original, root = fixtures
    before = _fingerprints(root)
    result, preview = cutover.rehearsal(source, snapshot, review, digest)
    after = _fingerprints(root)
    assert before == after
    assert source.read_bytes() == original
    assert result["result"] == "MEMORY_ONLY_ROLLBACK_VERIFIED"
    assert result["source_sha256"] == digest
    assert result["rollback_sha256"] == digest
    assert result["preview_sha256"] == hashlib.sha256(preview.encode("utf-8")).hexdigest()
    assert result["disk_writes"] == 0
    assert result["review_authenticity"] == "NOT_VERIFIED_BY_THIS_TOOL"
    assert result["semantic_accuracy"] == "NOT_VERIFIED_BY_THIS_TOOL"
    assert result["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert result["selected_count"] == 1
    assert "Synthetic reviewed current handoff" in preview
    assert "PRIVATE_SYNTHETIC_CHECKPOINT" not in preview


def test_default_command_prints_only_nonprivate_metadata(fixtures):
    source, snapshot, review, digest, _, root = fixtures
    before = _fingerprints(root)
    out = io.StringIO()
    with redirect_stdout(out):
        assert cutover.main([
            "--source", str(source),
            "--snapshot", str(snapshot),
            "--review-file", str(review),
            "--expected-source-sha256", digest,
        ]) == 0
    metadata = json.loads(out.getvalue())
    assert metadata["result"] == "MEMORY_ONLY_ROLLBACK_VERIFIED"
    assert "PRIVATE_SYNTHETIC_CHECKPOINT" not in out.getvalue()
    assert "Synthetic reviewed current handoff" not in out.getvalue()
    assert before == _fingerprints(root)


def test_explicit_render_outputs_reviewed_text_but_not_full_archive(fixtures):
    source, snapshot, review, digest, _, root = fixtures
    before = _fingerprints(root)
    out = io.StringIO()
    with redirect_stdout(out):
        assert cutover.main([
            "--source", str(source),
            "--snapshot", str(snapshot),
            "--review-file", str(review),
            "--expected-source-sha256", digest,
            "--render-reviewed-preview",
        ]) == 0
    assert "Synthetic reviewed current handoff" in out.getvalue()
    assert "PRIVATE_SYNTHETIC_CHECKPOINT" not in out.getvalue()
    assert before == _fingerprints(root)


def test_wrong_source_digest_fails_before_rehearsal(fixtures):
    source, snapshot, review, _, _, root = fixtures
    before = _fingerprints(root)
    with pytest.raises(ValueError, match="pre-migration pin"):
        cutover.rehearsal(source, snapshot, review, "0" * 64)
    assert before == _fingerprints(root)


@pytest.mark.parametrize("pin", ["not-a-digest", "A" * 64, ""])
def test_invalid_explicit_sha_pin_rejected(fixtures, pin):
    source, snapshot, review, _, _, root = fixtures
    before = _fingerprints(root)
    with pytest.raises(ValueError, match="full lowercase digest"):
        cutover.rehearsal(source, snapshot, review, pin)
    assert before == _fingerprints(root)


def test_source_mutation_after_snapshot_is_detected(fixtures):
    source, snapshot, review, digest, _, root = fixtures
    source.write_bytes(source.read_bytes() + b"POST_SNAPSHOT_MUTATION")
    before = _fingerprints(root)
    with pytest.raises(ValueError, match="pre-migration pin"):
        cutover.rehearsal(source, snapshot, review, digest)
    assert before == _fingerprints(root)


def test_review_provenance_not_matching_snapshot_fails(fixtures):
    source, snapshot, review, digest, _, root = fixtures
    body = json.loads(review.read_text(encoding="utf-8"))
    body["source_sha256"] = "0" * 64
    review.write_text(json.dumps(body), encoding="utf-8")
    before = _fingerprints(root)
    with pytest.raises(ValueError, match="different sources"):
        cutover.rehearsal(source, snapshot, review, digest)
    assert before == _fingerprints(root)


def test_forged_success_acceptance_label_is_rejected(fixtures):
    source, snapshot, review, digest, _, root = fixtures
    body = json.loads(review.read_text(encoding="utf-8"))
    body["entries"][0]["status"] = "PASS"
    review.write_text(json.dumps(body), encoding="utf-8")
    before = _fingerprints(root)
    with pytest.raises(ValueError, match="Unsupported reviewed status"):
        cutover.rehearsal(source, snapshot, review, digest)
    assert before == _fingerprints(root)


def test_corrupted_snapshot_never_passes_rollback(fixtures):
    source, snapshot, review, digest, _, _ = fixtures
    first_piece = sorted((snapshot / "pieces").glob("*.bin"))[0]
    first_piece.write_bytes(first_piece.read_bytes() + b"CORRUPTION")
    with pytest.raises(ValueError, match="mismatch"):
        cutover.rehearsal(source, snapshot, review, digest)


def test_missing_selection_file_fails_closed(fixtures):
    source, snapshot, review, digest, _, _ = fixtures
    review.unlink()
    with pytest.raises(ValueError, match="Review input"):
        cutover.rehearsal(source, snapshot, review, digest)


def test_reviewer_selection_growing_beyond_limit_rejected(fixtures):
    source, snapshot, review, digest, _, _ = fixtures
    review.write_bytes(b" " * 66_000)
    with pytest.raises(ValueError, match="maximum size"):
        cutover.rehearsal(source, snapshot, review, digest)


def test_missing_snapshot_fails_without_touching_source(fixtures):
    source, snapshot, review, digest, original, _ = fixtures
    renamed = snapshot.with_name(snapshot.name + "-missing")
    snapshot.rename(renamed)
    with pytest.raises(ValueError, match="Snapshot directory missing"):
        cutover.rehearsal(source, snapshot, review, digest)
    assert source.read_bytes() == original


def test_source_symlink_rejected_when_supported(fixtures, tmp_path):
    source, snapshot, review, digest, _, _ = fixtures
    shortcut = tmp_path / "symlink.md"
    try:
        shortcut.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("Windows symlink capability unavailable")
    with pytest.raises(ValueError, match="symbolic link"):
        cutover.rehearsal(shortcut, snapshot, review, digest)
