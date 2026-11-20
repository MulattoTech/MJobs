"""Synthetic, isolated B3 private preview persistence tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.verification import handoff_archive
from tools.verification import handoff_private_preview as private


@pytest.fixture
def scenario(tmp_path):
    # This file contains only synthetic text, never the real handoff.
    original = (
        b"\xef\xbb\xbfHISTORIC_SYNTHETIC_SECRET\r\n"
        b"## Legacy\r\n"
        b"An outdated status is not current acceptance\r\n"
        b"## Candidate\r\n"
        b"Reviewer must select the current state\r\n"
    )
    source = tmp_path / "sample-legacy.md"
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    root = tmp_path / "archive-root"
    root.mkdir()
    handoff_archive.create_archive(source, root, digest)
    archive = root / ("sha256-" + digest)
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    reviewer = tmp_path / "review.json"
    reviewer.write_text(
        json.dumps({
            "schema_version": 1,
            "source_sha256": digest,
            "reviewed_by": "Synthetic Reviewer",
            "reviewed_at_utc": "2026-11-20T20:00:00Z",
            "review_reference": "SYNTHETIC_REVIEW_ONLY",
            "entries": [{
                "subject": "Synthetic current",
                "checkpoint_id": manifest["checkpoints"][-1]["checkpoint_id"],
                "status": "UNKNOWN",
                "summary": "Current state has not been approved",
                "evidence": "Synthetic private fixture",
                "unknowns": "Hosted verification absent",
            }],
        }), encoding="utf-8",
    )
    destination = tmp_path / "private-output"
    destination.mkdir()
    return source, archive, reviewer, destination, digest, original, tmp_path


def args(scenario):
    source, archive, reviewer, _, digest, *_ = scenario
    return source, archive, reviewer, digest


def snapshot_bytes(scenario):
    source, archive, reviewer, destination, *_ = scenario
    return {
        "source": source.read_bytes(),
        "review": reviewer.read_bytes(),
        "manifest": (archive / "manifest.json").read_bytes(),
        "destination": {p.name for p in destination.iterdir()},
    }


def candidate(scenario, digest):
    return scenario[3] / ("review-preview-sha256-" + digest)


def test_plan_is_read_only_content_free_and_does_not_write(scenario, capsys):
    source, archive, reviewer, digest = args(scenario)
    before = snapshot_bytes(scenario)
    rc = private.main([
        "--source", str(source), "--snapshot", str(archive),
        "--review-file", str(reviewer), "--expected-source-sha256", digest,
    ])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "READ_ONLY_PRIVATE_PREVIEW_PLAN"
    assert result["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    assert result["source_sha256"] == digest
    assert len(result["preview_sha256"]) == 64
    assert "HISTORIC_SYNTHETIC_SECRET" not in json.dumps(result)
    assert "Current state has not been approved" not in json.dumps(result)
    assert before == snapshot_bytes(scenario)


def test_private_candidate_write_verify_no_source_or_reader_mutation(scenario):
    source, archive, reviewer, destination, digest, original, _ = scenario
    before = snapshot_bytes(scenario)
    result = private.create_candidate(source, archive, reviewer, digest, destination)
    assert result["result"] == "PRIVATE_PREVIEW_CREATED_NOT_ACTIVATED"
    target = candidate(scenario, result["preview_sha256"])
    assert target.is_dir()
    assert {p.name for p in target.iterdir()} == {"preview.md", "receipt.json"}
    preview = (target / "preview.md").read_bytes()
    assert b"Current state has not been approved" in preview
    assert b"HISTORIC_SYNTHETIC_SECRET" not in preview
    assert b"An outdated status" not in preview
    receipt = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["source_sha256"] == digest
    assert receipt["preview_sha256"] == hashlib.sha256(preview).hexdigest()
    assert receipt["canonical_cutover"] == "NOT_AUTHORIZED_OR_PERFORMED"
    checked = private.verify_candidate(
        target, expected_preview_sha256=result["preview_sha256"],
        expected_source_sha256=digest,
    )
    assert checked["result"] == "PRIVATE_PREVIEW_VERIFIED_NOT_ACTIVATED"
    assert source.read_bytes() == original
    assert reviewer.read_bytes() == before["review"]
    assert (archive / "manifest.json").read_bytes() == before["manifest"]
    assert not (destination / private._LOCK_NAME).exists()


def test_private_candidate_survives_source_deletion_and_fresh_python_process(scenario):
    source, archive, reviewer, destination, digest, _, root = scenario
    created = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, created["preview_sha256"])
    source.unlink()
    cmd = [
        sys.executable, "-m", "tools.verification.handoff_private_preview",
        "--verify", "--candidate", str(target),
        "--expected-source-sha256", digest,
        "--expected-review-sha256", created["review_sha256"],
        "--expected-preview-sha256", created["preview_sha256"],
    ]
    cwd = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=20, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    verified = json.loads(proc.stdout)
    assert verified["result"] == "PRIVATE_PREVIEW_VERIFIED_NOT_ACTIVATED"
    assert verified["source_sha256"] == digest
    assert not (root / "planning" / "SESSION_HANDOFF.md").exists()


def test_idempotent_existing_candidate_does_not_replace_bytes_or_timestamp(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    first = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, first["preview_sha256"])
    file = target / "preview.md"
    before = (file.read_bytes(), file.stat().st_mtime_ns)
    second = private.create_candidate(source, archive, reviewer, digest, destination)
    assert second["result"] == "EXISTING_PRIVATE_PREVIEW_VERIFIED"
    assert before == (file.read_bytes(), file.stat().st_mtime_ns)
    assert len(list(destination.iterdir())) == 1


@pytest.mark.parametrize("sha", ["invalid", "A" * 64, "0" * 64])
def test_bad_or_stale_source_pin_fails_before_write(scenario, sha):
    source, archive, reviewer, destination, _, original, _ = scenario
    with pytest.raises(ValueError, match="SHA-256|pin"):
        private.create_candidate(source, archive, reviewer, sha, destination)
    assert list(destination.iterdir()) == []
    assert source.read_bytes() == original


def test_tampered_preview_fails_verification_and_cannot_be_replaced(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    data = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, data["preview_sha256"])
    preview = target / "preview.md"
    preview.write_bytes(preview.read_bytes() + b"MALICIOUS_EDIT")
    with pytest.raises(ValueError, match="corrupted"):
        private.verify_candidate(target, expected_preview_sha256=data["preview_sha256"])
    with pytest.raises(ValueError, match="corrupted"):
        private.create_candidate(source, archive, reviewer, digest, destination)
    assert preview.read_bytes().endswith(b"MALICIOUS_EDIT")


def test_unknown_receipt_fields_are_not_accepted(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    data = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, data["preview_sha256"])
    receipt = target / "receipt.json"
    raw = json.loads(receipt.read_text(encoding="utf-8"))
    raw["unreviewed_acceptance"] = "PASS"
    receipt.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="missing or unknown fields"):
        private.verify_candidate(target, expected_preview_sha256=data["preview_sha256"])


def test_unrecognized_or_missing_directory_files_fail_closed(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    created = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, created["preview_sha256"])
    (target / "unexpected.txt").write_bytes(b"EXTRA")
    with pytest.raises(ValueError, match="unexpected or missing"):
        private.verify_candidate(target, expected_preview_sha256=created["preview_sha256"])
    (target / "unexpected.txt").unlink()
    (target / "preview.md").unlink()
    with pytest.raises(ValueError, match="unexpected or missing"):
        private.verify_candidate(target, expected_preview_sha256=created["preview_sha256"])


def test_candidate_name_and_expected_hash_must_match(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    result = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, result["preview_sha256"])
    with pytest.raises(ValueError, match="pinned preview"):
        private.verify_candidate(target, expected_preview_sha256="0" * 64)
    with pytest.raises(ValueError, match="expected source"):
        private.verify_candidate(
            target, expected_preview_sha256=result["preview_sha256"],
            expected_source_sha256="0" * 64,
        )


def test_foreign_writer_lock_is_not_removed_on_failure(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    lock = destination / private._LOCK_NAME
    lock.write_bytes(b"FOREIGN_LOCK")
    with pytest.raises(ValueError, match="Existing preview writer lock"):
        private.create_candidate(source, archive, reviewer, digest, destination)
    assert lock.read_bytes() == b"FOREIGN_LOCK"
    assert len(list(destination.iterdir())) == 1


def test_partial_staging_failure_cleans_owned_stage_and_lock(scenario, monkeypatch):
    source, archive, reviewer, destination, digest, original, _ = scenario
    real = private._write_exclusive

    def corrupt_interrupted_write(path, contents):
        if Path(path).name == "receipt.json":
            raise OSError("synthetic interrupted receipt write")
        return real(path, contents)

    monkeypatch.setattr(private, "_write_exclusive", corrupt_interrupted_write)
    with pytest.raises(OSError, match="synthetic interrupted"):
        private.create_candidate(source, archive, reviewer, digest, destination)
    assert list(destination.iterdir()) == []
    assert source.read_bytes() == original


def test_source_changed_during_staging_fails_and_cleans_owned_stage(scenario, monkeypatch):
    source, archive, reviewer, destination, digest, original, _ = scenario
    real = private._write_exclusive
    altered = [False]

    def mutate_source_once(path, contents):
        result = real(path, contents)
        if Path(path).name == "receipt.json" and not altered[0]:
            source.write_bytes(original + b"CONCURRENT_SOURCE_MUTATION")
            altered[0] = True
        return result

    monkeypatch.setattr(private, "_write_exclusive", mutate_source_once)
    with pytest.raises(ValueError, match="pin"):
        private.create_candidate(source, archive, reviewer, digest, destination)
    assert list(destination.iterdir()) == []
    assert source.read_bytes().endswith(b"CONCURRENT_SOURCE_MUTATION")


def test_review_changed_during_staging_fails_and_does_not_publish(scenario, monkeypatch):
    source, archive, reviewer, destination, digest, original, _ = scenario
    real = private._write_exclusive
    altered = [False]

    def mutate_review_once(path, contents):
        result = real(path, contents)
        if Path(path).name == "receipt.json" and not altered[0]:
            reviewer.write_bytes(reviewer.read_bytes() + b" ")
            altered[0] = True
        return result

    monkeypatch.setattr(private, "_write_exclusive", mutate_review_once)
    with pytest.raises(ValueError, match="changed|review"):
        private.create_candidate(source, archive, reviewer, digest, destination)
    assert list(destination.iterdir()) == []
    assert source.read_bytes() == original


def test_private_destination_cannot_be_inside_archive(scenario):
    source, archive, reviewer, _, digest, original, _ = scenario
    with pytest.raises(ValueError, match="inside the source archive"):
        private.create_candidate(source, archive, reviewer, digest, archive)
    assert source.read_bytes() == original


def test_private_destination_cannot_be_inside_any_git_checkout(scenario):
    source, archive, reviewer, _, digest, original, root = scenario
    other = root / "some-other-checkout"
    (other / ".git").mkdir(parents=True)
    destination = other / "private-output"
    destination.mkdir()
    with pytest.raises(ValueError, match="Git checkout"):
        private.create_candidate(source, archive, reviewer, digest, destination)
    assert source.read_bytes() == original
    assert list(destination.iterdir()) == []


def test_symlinked_destination_refused_if_supported(scenario):
    source, archive, reviewer, destination, digest, original, root = scenario
    link = root / "output-link"
    try:
        link.symlink_to(destination, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("Symlink support unavailable")
    with pytest.raises(ValueError, match="symlink"):
        private.create_candidate(source, archive, reviewer, digest, link)
    assert source.read_bytes() == original


def test_explicit_cli_write_is_metadata_only(scenario, capsys):
    source, archive, reviewer, destination, digest, original, _ = scenario
    assert private.main([
        "--source", str(source), "--snapshot", str(archive),
        "--review-file", str(reviewer), "--expected-source-sha256", digest,
        "--destination", str(destination), "--write",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "PRIVATE_PREVIEW_CREATED_NOT_ACTIVATED"
    assert "HISTORIC_SYNTHETIC_SECRET" not in json.dumps(output)
    assert "Current state has not been approved" not in json.dumps(output)
    assert source.read_bytes() == original


def test_all_nonmatching_cli_modes_reject_unused_options(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    plan_args = [
        "--source", str(source), "--snapshot", str(archive),
        "--review-file", str(reviewer), "--expected-source-sha256", digest,
    ]
    with pytest.raises(SystemExit):
        private.main([*plan_args, "--destination", str(destination)])
    with pytest.raises(SystemExit):
        private.main([*plan_args, "--expected-preview-sha256", "0" * 64])
    with pytest.raises(SystemExit):
        private.main([*plan_args, "--candidate", str(destination)])
    with pytest.raises(SystemExit):
        private.main([*plan_args, "--write", "--destination", str(destination),
                      "--candidate", str(destination)])
    with pytest.raises(SystemExit):
        private.main(["--verify", "--candidate", str(destination),
                      "--expected-preview-sha256", "0" * 64,
                      "--review-file", str(reviewer)])
    assert list(destination.iterdir()) == []


def test_private_preview_files_created_with_restrictive_mode_on_posix(scenario):
    import stat
    if os.name != "posix":
        pytest.skip("POSIX permission bit assertions require a POSIX runner")
    source, archive, reviewer, destination, digest, _, _ = scenario
    result = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, result["preview_sha256"])
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "preview.md").stat().st_mode) == 0o600
    assert stat.S_IMODE((target / "receipt.json").stat().st_mode) == 0o600


def test_fake_active_cutover_claim_in_receipt_is_rejected(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    result = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, result["preview_sha256"])
    receipt = target / "receipt.json"
    document = json.loads(receipt.read_text(encoding="utf-8"))
    document["canonical_cutover"] = "ACTIVE"
    receipt.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="may not claim"):
        private.verify_candidate(target, expected_preview_sha256=result["preview_sha256"])


def test_standalone_verifier_requires_all_three_pinned_digests(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    result = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, result["preview_sha256"])
    incomplete = [
        "--verify", "--candidate", str(target),
        "--expected-preview-sha256", result["preview_sha256"],
        "--expected-source-sha256", digest,
    ]
    with pytest.raises(SystemExit) as exc:
        private.main(incomplete)
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        private.main([
            "--verify", "--candidate", str(target),
            "--expected-preview-sha256", result["preview_sha256"],
            "--expected-review-sha256", result["review_sha256"],
        ])
    assert target.is_dir()


def test_modified_receipt_review_digest_rejected_against_explicit_pin(scenario):
    source, archive, reviewer, destination, digest, _, _ = scenario
    result = private.create_candidate(source, archive, reviewer, digest, destination)
    target = candidate(scenario, result["preview_sha256"])
    receipt = target / "receipt.json"
    record = json.loads(receipt.read_text(encoding="utf-8"))
    record["review_sha256"] = "0" * 64
    receipt.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="expected review SHA-256"):
        private.verify_candidate(
            target, expected_preview_sha256=result["preview_sha256"],
            expected_source_sha256=digest,
            expected_review_sha256=result["review_sha256"],
        )
