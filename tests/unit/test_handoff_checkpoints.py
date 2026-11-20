"""Lossless recovery characterization for future B3 handoff checkpoints."""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path

import pytest

from tools.verification.handoff_checkpoints import (
    build_verified_plan,
    main,
    manifest_for,
    recover_exact,
    split_handoff,
)


def test_mixed_line_endings_and_fences_reconstruct_exact_bytes():
    original = (
        b"\xef\xbb\xbfLegacy preamble\r\n"
        b"## Same heading\r\n"
        b"first payload\r\n"
        b"~~~python\n"
        b"## not a heading in fenced code\n"
        b"~~~\r\n"
        b"### a nested heading stays with its parent\n"
        b"# Other top-level heading\n"
        b"second payload\n"
        b"## Same heading\r\n"
        b"third payload without terminal newline"
    )
    sections = split_handoff(original)
    assert [section.heading_level for section in sections] == [None, 2, 1, 2]
    assert sections[1].payload.startswith(b"## Same heading")
    assert b"## not a heading" in sections[1].payload
    assert b"### a nested" in sections[1].payload
    assert sections[-1].payload.endswith(b"without terminal newline")
    plan = manifest_for(original, sections)
    assert plan["source_length"] == len(original)
    assert plan["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert plan == build_verified_plan(original)
    assert recover_exact([section.payload for section in sections], plan) == original
    assert all("payload" not in entry for entry in plan["checkpoints"])


def test_backtick_fence_does_not_create_false_checkpoint():
    raw = b"## First\n" + bytes([96]) * 3 + b"python\n# fake\n## fake\n" + bytes([96]) * 3 + b"\n## Last\n"
    blocks = split_handoff(raw)
    assert len(blocks) == 2
    assert blocks[0].payload.endswith(bytes([96]) * 3 + b"\n")
    assert recover_exact([b.payload for b in blocks], manifest_for(raw, blocks)) == raw


@pytest.mark.parametrize("raw,expected_count", [
    (b"", 0),
    (b"no headings", 1),
    (b"## First\n## Second\n", 2),
    (b"# First\r\n## Second\r\n", 2),
    (b"### nested only\n### still nested\n", 1),
])
def test_small_and_empty_sources_are_deterministic(raw, expected_count):
    first = build_verified_plan(raw)
    second = build_verified_plan(raw)
    assert first == second
    assert first["checkpoint_count"] == expected_count
    assert recover_exact([x.payload for x in split_handoff(raw)], first) == raw


def test_corrupted_or_missing_checkpoints_fail_closed():
    raw = b"## First\nalpha\n## Second\nbravo\n"
    blocks = split_handoff(raw)
    plan = manifest_for(raw, blocks)
    chunks = [block.payload for block in blocks]

    with pytest.raises(ValueError, match="Checkpoint count mismatch"):
        recover_exact(chunks[:-1], plan)

    with pytest.raises(ValueError, match="Checkpoint identity or offset mismatch"):
        recover_exact([chunks[1], chunks[0]], plan)

    with pytest.raises(ValueError, match="Checkpoint identity or offset mismatch"):
        recover_exact([chunks[0] + b"!", chunks[1]], plan)

    modified = dict(plan)
    modified["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Source recovery digest mismatch"):
        recover_exact(chunks, modified)

    modified = dict(plan)
    modified["schema_version"] = 9
    with pytest.raises(ValueError, match="Unsupported checkpoint schema"):
        recover_exact(chunks, modified)

    modified = json.loads(json.dumps(plan))
    modified["checkpoints"][0]["heading_level"] = 1
    with pytest.raises(ValueError, match="Checkpoint segmentation mismatch"):
        recover_exact(chunks, modified)


def test_metadata_only_json_and_quiet_status_do_not_write(tmp_path):
    source = tmp_path / "handoff.md"
    original = b"## A\r\nfirst\n## B\nlast"
    source.write_bytes(original)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    quiet = io.StringIO()
    with redirect_stdout(quiet):
        assert main(["--source", str(source)]) == 0
    assert "PASS" in quiet.getvalue()

    report = io.StringIO()
    with redirect_stdout(report):
        assert main(["--source", str(source), "--json"]) == 0
    data = json.loads(report.getvalue())
    assert data["source_length"] == len(original)
    assert data["checkpoint_count"] == 2
    assert "first" not in report.getvalue()
    assert "last" not in report.getvalue()

    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert after == before


def test_actual_repository_handoff_has_lossless_recovery():
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "planning" / "SESSION_HANDOFF.md").read_bytes()
    plan = build_verified_plan(source)
    assert plan["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert plan["checkpoint_count"] > 0
    assert recover_exact(
        [section.payload for section in split_handoff(source)],
        plan,
    ) == source
