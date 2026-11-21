"""Synthetic, read-only tests for reviewed B3 current-summary previews."""

from __future__ import annotations

import hashlib
import json

import pytest

from tools.verification import handoff_archive, handoff_current_summary as current


@pytest.fixture
def reviewed(tmp_path):
    original = (
        b"## PREVIOUS PRIVATE_SYNTHETIC_RECORD\n"
        b"Historic state should never be selected automatically.\n"
        b"## CURRENT SYNTHETIC RECORD\r\n"
        b"Only the reviewer can certify its meaning.\r\n"
    )
    source = tmp_path / "old-handoff.md"
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    root = tmp_path / "archive-root"
    root.mkdir()
    handoff_archive.create_archive(source, root, digest)
    snapshot = root / ("sha256-" + digest)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    first, second = [entry["checkpoint_id"] for entry in manifest["checkpoints"]]
    record = {
        "schema_version": 1,
        "source_sha256": digest,
        "reviewed_by": "Synthetic Review Operator",
        "reviewed_at_utc": "2026-11-20T18:00:00Z",
        "review_reference": "https://github.com/MulattoTech/MJobs/pull/1070",
        "entries": [
            {
                "subject": "Source readiness",
                "checkpoint_id": second,
                "status": "ACTIVE",
                "summary": "Reviewed current source context.",
                "evidence": "Synthetic bounded evidence 42",
                "unknowns": "Source deployment remains unverified",
            },
            {
                "subject": "Past verification",
                "checkpoint_id": first,
                "status": "UNKNOWN",
                "summary": "Historic review, not current acceptance.",
                "evidence": "Synthetic note only",
                "unknowns": "No current hosted verification",
            },
        ],
    }
    review_file = tmp_path / "review.json"
    review_file.write_text(json.dumps(record), encoding="utf-8")
    return {
        "source": source,
        "original": original,
        "digest": digest,
        "snapshot": snapshot,
        "record": record,
        "review_file": review_file,
    }


def _data(record):
    return json.dumps(record, sort_keys=True).encode("utf-8")


def test_explicit_review_renders_only_selected_claims_not_archive_text(reviewed):
    meta, text = current.make_review_preview(
        reviewed["snapshot"], _data(reviewed["record"]))
    assert meta["result"] == "REVIEWED_PREVIEW_NOT_CANONICAL"
    assert meta["source_sha256"] == reviewed["digest"]
    assert meta["selected_count"] == 2
    assert text.startswith("# MJobs Current Handoff")
    assert "NOT canonical acceptance" in text
    assert "Source readiness" in text
    assert "Past verification" in text
    assert "Source deployment remains unverified" in text
    assert "PRIVATE_SYNTHETIC_RECORD" not in text
    assert "Historic state should never" not in text
    assert reviewed["original"] == reviewed["source"].read_bytes()


def test_default_cli_prints_only_metadata_and_modifies_no_files(reviewed, capsys):
    review_bytes = reviewed["review_file"].read_bytes()
    manifest = (reviewed["snapshot"] / "manifest.json").read_bytes()
    assert current.main([
        "--snapshot", str(reviewed["snapshot"]),
        "--review-file", str(reviewed["review_file"]),
    ]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["selected_count"] == 2
    assert out["source_sha256"] == reviewed["digest"]
    assert "Reviewed current source context." not in json.dumps(out)
    assert "PRIVATE_SYNTHETIC_RECORD" not in json.dumps(out)
    assert reviewed["review_file"].read_bytes() == review_bytes
    assert (reviewed["snapshot"] / "manifest.json").read_bytes() == manifest


def test_render_requires_explicit_flag_and_uses_no_automatic_latest_selection(reviewed, capsys):
    assert current.main([
        "--snapshot", str(reviewed["snapshot"]),
        "--review-file", str(reviewed["review_file"]),
        "--render",
    ]) == 0
    text = capsys.readouterr().out
    assert "Reviewed current source context." in text
    assert "Historic review, not current acceptance." in text
    assert "NOT canonical acceptance" in text
    assert "PRIVATE_SYNTHETIC_RECORD" not in text


def test_wrong_archive_digest_fails_closed(reviewed):
    record = dict(reviewed["record"])
    record["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="different sources"):
        current.make_review_preview(reviewed["snapshot"], _data(record))


def test_unknown_checkpoint_id_fails_closed(reviewed):
    record = json.loads(json.dumps(reviewed["record"]))
    record["entries"][0]["checkpoint_id"] = "99999-0000000000000000"
    with pytest.raises(ValueError, match="unknown checkpoint"):
        current.make_review_preview(reviewed["snapshot"], _data(record))


def test_duplicate_subjects_are_not_silently_deduplicated(reviewed):
    record = json.loads(json.dumps(reviewed["record"]))
    record["entries"][1]["subject"] = "SOURCE READINESS"
    with pytest.raises(ValueError, match="Duplicate reviewed subject"):
        current.make_review_preview(reviewed["snapshot"], _data(record))


def test_pass_is_not_a_valid_reviewed_workflow_status(reviewed):
    record = json.loads(json.dumps(reviewed["record"]))
    record["entries"][0]["status"] = "PASS"
    with pytest.raises(ValueError, match="Unsupported reviewed status"):
        current.make_review_preview(reviewed["snapshot"], _data(record))


@pytest.mark.parametrize("field", ["summary", "evidence", "unknowns", "subject"])
def test_missing_required_review_evidence_is_rejected(reviewed, field):
    record = json.loads(json.dumps(reviewed["record"]))
    record["entries"][0][field] = ""
    with pytest.raises(ValueError, match=field):
        current.make_review_preview(reviewed["snapshot"], _data(record))


@pytest.mark.parametrize("bad_date", [
    "2026-11-20T18:00:00-06:00",
    "2026-11-20 18:00:00",
    "not a date",
])
def test_review_timestamp_requires_explicit_utc(reviewed, bad_date):
    record = dict(reviewed["record"])
    record["reviewed_at_utc"] = bad_date
    with pytest.raises(ValueError, match="UTC|reviewed_at_utc"):
        current.make_review_preview(reviewed["snapshot"], _data(record))


def test_unknown_review_schema_fields_fail_closed(reviewed):
    record = dict(reviewed["record"])
    record["pretend_approval"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        current.make_review_preview(reviewed["snapshot"], _data(record))


def test_empty_or_oversized_review_selection_rejected(reviewed):
    empty = dict(reviewed["record"])
    empty["entries"] = []
    with pytest.raises(ValueError, match="bounded nonempty"):
        current.make_review_preview(reviewed["snapshot"], _data(empty))
    with pytest.raises(ValueError, match="bounded input size"):
        current.make_review_preview(reviewed["snapshot"], b" " * 70_000)


def test_review_text_cannot_inject_markdown_html_or_tables(reviewed):
    record = json.loads(json.dumps(reviewed["record"]))
    record["entries"][0]["subject"] = "Source | [fake](https://example.invalid)"
    record["entries"][0]["summary"] = "<script>*not markup*</script> | status"
    _, preview = current.make_review_preview(reviewed["snapshot"], _data(record))
    assert "<script>" not in preview
    assert "\\<script\\>" in preview
    assert "Source \\|" in preview
    assert "\\*not markup\\*" in preview


def test_control_characters_and_bidi_spoofing_rejected(reviewed):
    for value in ["line\nbreak", "bad\tfield", "spoof\u202evalue"]:
        record = json.loads(json.dumps(reviewed["record"]))
        record["entries"][0]["summary"] = value
        with pytest.raises(ValueError, match="summary"):
            current.make_review_preview(reviewed["snapshot"], _data(record))


def test_corrupt_snapshot_cannot_authorize_preview(reviewed):
    piece = sorted((reviewed["snapshot"] / "pieces").glob("*.bin"))[0]
    piece.write_bytes(piece.read_bytes() + b"TAMPER")
    with pytest.raises(ValueError):
        current.make_review_preview(reviewed["snapshot"], _data(reviewed["record"]))


def test_review_input_symlink_rejected_at_cli_boundary(reviewed, tmp_path):
    link = tmp_path / "review-link.json"
    try:
        link.symlink_to(reviewed["review_file"])
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable in current Windows environment")
    with pytest.raises(SystemExit):
        current.main([
            "--snapshot", str(reviewed["snapshot"]),
            "--review-file", str(link),
        ])


def test_empty_review_reference_rejected(reviewed):
    record = dict(reviewed["record"])
    record["review_reference"] = ""
    with pytest.raises(ValueError, match="review_reference"):
        current.make_review_preview(reviewed["snapshot"], _data(record))
