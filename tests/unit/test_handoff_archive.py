"""Synthetic-only test fixtures for the B3 immutable archive writer.

No test exports the real historical handoff.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.verification import handoff_archive as archive


@pytest.fixture
def sample(tmp_path):
    source = tmp_path / "legacy-handoff.md"
    original = (
        b"\xef\xbb\xbfPRIVATE_SYNTHETIC_TEXT\r\n"
        b"## First record\r\n"
        b"alpha\r\n"
        b"~~~txt\n## fenced, not a checkpoint\n~~~\n"
        b"## Second record\n"
        b"beta without final newline"
    )
    source.write_bytes(original)
    destination = tmp_path / "private-archive-root"
    destination.mkdir()
    return source, destination, original, hashlib.sha256(original).hexdigest()


def _snapshot(destination, digest):
    return destination / f"sha256-{digest}"


def test_create_verify_and_recover_exact_bytes_without_modifying_source(sample):
    source, dest, original, digest = sample
    result = archive.create_archive(source, dest, digest)
    assert result == {
        "result": "SNAPSHOT_CREATED",
        "source_sha256": digest,
        "source_length": len(original),
        "checkpoint_count": 3,
    }
    snapshot = _snapshot(dest, digest)
    verified = archive.verify_archive(snapshot)
    assert verified["result"] == "ARCHIVE_VERIFIED"
    recovered, manifest = archive._verify_archive(snapshot)
    assert recovered == original
    assert manifest["source_sha256"] == digest
    assert source.read_bytes() == original
    assert not (dest / archive._LOCK).exists()
    assert not any(x.name.startswith(".staging-") for x in dest.iterdir())
    assert b"PRIVATE_SYNTHETIC_TEXT" not in (snapshot / "manifest.json").read_bytes()


def test_source_can_disappear_and_archive_still_recovers(sample):
    source, dest, original, digest = sample
    archive.create_archive(source, dest, digest)
    source.unlink()
    recovered, manifest = archive._verify_archive(_snapshot(dest, digest))
    assert recovered == original
    assert manifest["source_length"] == len(original)


def test_repeated_write_is_read_only_and_idempotent(sample):
    source, dest, original, digest = sample
    archive.create_archive(source, dest, digest)
    snapshot = _snapshot(dest, digest)
    manifest = snapshot / "manifest.json"
    before = (manifest.read_bytes(), manifest.stat().st_mtime_ns)
    result = archive.create_archive(source, dest, digest)
    assert result["result"] == "ALREADY_VERIFIED"
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == before
    assert source.read_bytes() == original
    assert len(list(dest.iterdir())) == 1


@pytest.mark.parametrize("pin", ["bad", "0" * 64, "A" * 64])
def test_invalid_source_pin_prevents_any_archive_write(sample, pin):
    source, dest, original, _ = sample
    with pytest.raises(ValueError, match="SHA-256|source pin"):
        archive.create_archive(source, dest, pin)
    assert list(dest.iterdir()) == []
    assert source.read_bytes() == original


def test_tampered_checkpoint_fails_closed_and_cannot_be_overwritten(sample):
    source, dest, original, digest = sample
    archive.create_archive(source, dest, digest)
    piece = sorted((_snapshot(dest, digest) / "pieces").glob("*.bin"))[0]
    piece.write_bytes(piece.read_bytes() + b"TAMPER")
    with pytest.raises(ValueError, match="identity|offset|checksum"):
        archive.verify_archive(_snapshot(dest, digest))
    with pytest.raises(ValueError):
        archive.create_archive(source, dest, digest)
    assert piece.read_bytes().endswith(b"TAMPER")
    assert source.read_bytes() == original


def test_missing_and_extra_checkpoint_fail_closed(sample):
    source, dest, original, digest = sample
    archive.create_archive(source, dest, digest)
    parts = _snapshot(dest, digest) / "pieces"
    first = sorted(parts.glob("*.bin"))[0]
    backup = first.read_bytes()
    first.unlink()
    with pytest.raises(ValueError, match="Missing or symbolic checkpoint"):
        archive.verify_archive(_snapshot(dest, digest))
    first.write_bytes(backup)
    (parts / "UNRECOGNIZED.bin").write_bytes(b"data")
    with pytest.raises(ValueError, match="Unexpected or missing"):
        archive.verify_archive(_snapshot(dest, digest))
    assert source.read_bytes() == original


def test_manifest_path_forgery_is_rejected_before_file_access(sample):
    source, dest, _, digest = sample
    archive.create_archive(source, dest, digest)
    manifest = _snapshot(dest, digest) / "manifest.json"
    body = json.loads(manifest.read_text(encoding="utf-8"))
    body["checkpoints"][0]["checkpoint_id"] = "../traversal-attempt"
    manifest.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe checkpoint identifier"):
        archive.verify_archive(_snapshot(dest, digest))


def test_existing_unowned_writer_lock_is_preserved(sample):
    source, dest, original, digest = sample
    lock = dest / archive._LOCK
    lock.write_bytes(b"OTHER_WRITER_LOCK")
    with pytest.raises(ValueError, match="existing writer lock"):
        archive.create_archive(source, dest, digest)
    assert lock.read_bytes() == b"OTHER_WRITER_LOCK"
    assert not _snapshot(dest, digest).exists()
    assert source.read_bytes() == original


def test_partial_write_failure_rolls_back_only_owned_stage(sample, monkeypatch):
    source, dest, original, digest = sample
    original_write = archive._write_exclusive
    count = 0

    def fail_second_piece(path, content):
        nonlocal count
        if path.suffix == ".bin":
            count += 1
            if count == 2:
                raise OSError("synthetic partial write")
        return original_write(path, content)

    monkeypatch.setattr(archive, "_write_exclusive", fail_second_piece)
    with pytest.raises(OSError, match="synthetic partial write"):
        archive.create_archive(source, dest, digest)
    assert list(dest.iterdir()) == []
    assert source.read_bytes() == original


def test_source_modified_during_write_is_not_overwritten(sample, monkeypatch):
    source, dest, original, digest = sample
    original_write = archive._write_exclusive
    modified = False

    def modify_source_once(path, content):
        nonlocal modified
        result = original_write(path, content)
        if path.suffix == ".bin" and not modified:
            source.write_bytes(original + b"EXTERNAL_EDIT")
            modified = True
        return result

    monkeypatch.setattr(archive, "_write_exclusive", modify_source_once)
    with pytest.raises(ValueError, match="Source changed"):
        archive.create_archive(source, dest, digest)
    assert source.read_bytes().endswith(b"EXTERNAL_EDIT")
    assert list(dest.iterdir()) == []


def test_refuses_archive_root_inside_its_own_repository(sample):
    source, _, original, digest = sample
    with pytest.raises(ValueError, match="repository"):
        archive.create_archive(source, archive.REPO_ROOT, digest)
    assert source.read_bytes() == original


def test_refuses_archive_root_inside_any_other_git_worktree(sample, tmp_path):
    source, _, original, digest = sample
    fake_repo = tmp_path / "another-git-checkout"
    (fake_repo / ".git").mkdir(parents=True)
    destination = fake_repo / "archives"
    destination.mkdir()
    with pytest.raises(ValueError, match="Git checkout"):
        archive.create_archive(source, destination, digest)
    assert list(destination.iterdir()) == []
    assert source.read_bytes() == original


def test_rejects_symlink_archive_destination(sample, tmp_path):
    source, dest, original, digest = sample
    link = tmp_path / "linked-destination"
    try:
        link.symlink_to(dest, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable in this environment")
    with pytest.raises(ValueError, match="symlink"):
        archive.create_archive(source, link, digest)
    assert source.read_bytes() == original


def test_default_cli_is_metadata_only_and_has_no_side_effects(sample, capsys):
    source, dest, original, digest = sample
    assert archive.main(["--source", str(source)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "READ_ONLY_PLAN"
    assert report["source_sha256"] == digest
    assert "PRIVATE_SYNTHETIC_TEXT" not in json.dumps(report)
    assert list(dest.iterdir()) == []
    assert source.read_bytes() == original


def test_explicit_cli_write_and_verify_without_leaking_content(sample, capsys):
    source, dest, original, digest = sample
    assert archive.main([
        "--source", str(source), "--destination", str(dest),
        "--expected-sha256", digest, "--write",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["result"] == "SNAPSHOT_CREATED"
    assert "PRIVATE_SYNTHETIC_TEXT" not in json.dumps(created)
    assert archive.main([
        "--destination", str(_snapshot(dest, digest)), "--verify",
    ]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["source_sha256"] == digest
    assert verified["result"] == "ARCHIVE_VERIFIED"
    assert source.read_bytes() == original


def test_empty_source_round_trip(tmp_path):
    source = tmp_path / "empty.md"
    source.write_bytes(b"")
    dest = tmp_path / "archive"
    dest.mkdir()
    digest = hashlib.sha256(b"").hexdigest()
    result = archive.create_archive(source, dest, digest)
    assert result["checkpoint_count"] == 0
    recovered, manifest = archive._verify_archive(_snapshot(dest, digest))
    assert recovered == b""
    assert manifest["checkpoints"] == []


def test_unknown_snapshot_root_file_fails_closed(sample):
    source, dest, original, digest = sample
    archive.create_archive(source, dest, digest)
    snapshot = _snapshot(dest, digest)
    unexpected = snapshot / "unreviewed-extra.txt"
    unexpected.write_bytes(b"not a checkpoint")
    with pytest.raises(ValueError, match="Unexpected archive root entries"):
        archive.verify_archive(snapshot)
    assert unexpected.read_bytes() == b"not a checkpoint"
    assert source.read_bytes() == original
