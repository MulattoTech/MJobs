"""Synthetic acceptance for generated-temporary retention safety gates."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from workers.transient_retention import run_with_config


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
PRIVATE_CONTENT = b"SYNTHETIC_GENERATED_TEMPORARY_PRIVATE_CONTENT"


def _write_file(path, data, *, age_days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    modified = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(path, (modified, modified))
    return path


def _config(root, *, dry_run, prune_enabled):
    managed = root / "transient" / "generated"
    summary = root / ".retention" / "last-sweep.json"
    return {
        "configured_root": str(root),
        "allowed_roots": [str(root)],
        "rules": [{
            "id": "generated-temporary-files",
            "category": "GENERATED_TEMPORARY",
            "relative_root": "transient/generated",
            "pattern": "generated-*.tmp",
            "max_age_days": 5,
        }],
        "summary_path": str(summary),
        "dry_run": dry_run,
        "prune_enabled": prune_enabled,
    }


def test_generated_temporary_pruning_requires_explicit_gate_and_preserves_other_data(tmp_path):
    root = tmp_path / "configured"
    managed = root / "transient" / "generated"
    expired = _write_file(managed / "generated-old.tmp", PRIVATE_CONTENT, age_days=6)
    fresh = _write_file(managed / "generated-fresh.tmp", b"fresh synthetic temporary", age_days=2)
    unknown = _write_file(managed / "keep-not-matching.bin", b"unknown synthetic file", age_days=30)
    nested = _write_file(
        managed / "nested" / "generated-nested.tmp", b"nested synthetic file", age_days=30,
    )
    outside = _write_file(
        tmp_path / "outside" / "generated-outside.tmp", b"outside synthetic file", age_days=30,
    )
    history = _write_file(
        root / "applications" / "history.json", b"core application history", age_days=30,
    )

    preview = run_with_config(_config(root, dry_run=True, prune_enabled=True), now=NOW)
    assert preview["status"] == "OK"
    assert preview["dry_run"] is True
    assert preview["candidate_count"] == 1
    assert preview["removed_count"] == 0
    assert expired.exists()

    disabled = run_with_config(_config(root, dry_run=False, prune_enabled=False), now=NOW)
    assert disabled["status"] == "OK"
    assert disabled["dry_run"] is True
    assert disabled["candidate_count"] == 1
    assert disabled["removed_count"] == 0
    assert expired.exists()

    live = run_with_config(_config(root, dry_run=False, prune_enabled=True), now=NOW)
    assert live["status"] == "OK"
    assert live["dry_run"] is False
    assert live["candidate_count"] == 1
    assert live["removed_count"] == 1
    assert live["removed_bytes"] == len(PRIVATE_CONTENT)
    assert not expired.exists()
    for preserved in (fresh, unknown, nested, outside, history):
        assert preserved.exists()

    receipt_path = Path(_config(root, dry_run=False, prune_enabled=True)["summary_path"])
    receipt = receipt_path.read_text(encoding="utf-8")
    assert json.loads(receipt) == live
    for marker in (
        "generated-old.tmp", "generated-fresh.tmp", "generated-nested.tmp",
        "generated-outside.tmp", PRIVATE_CONTENT.decode(), str(root),
    ):
        assert marker not in receipt
    forbidden_keys = {"name", "filename", "path", "content", "payload"}

    def assert_receipt_has_no_file_details(value):
        if isinstance(value, dict):
            assert not (set(value) & forbidden_keys)
            for nested_value in value.values():
                assert_receipt_has_no_file_details(nested_value)
        elif isinstance(value, list):
            for nested_value in value:
                assert_receipt_has_no_file_details(nested_value)

    assert_receipt_has_no_file_details(json.loads(receipt))


def test_generated_temporary_symlink_and_target_are_preserved_without_elevation(tmp_path):
    root = tmp_path / "configured"
    managed = root / "transient" / "generated"
    managed.mkdir(parents=True)
    outside = _write_file(
        tmp_path / "outside" / "generated-target.tmp", b"synthetic outside target", age_days=20,
    )
    link = managed / "generated-linked.tmp"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"non-elevated symlink creation unavailable: {type(error).__name__}")

    result = run_with_config(_config(root, dry_run=False, prune_enabled=True), now=NOW)

    assert result["status"] == "OK"
    assert result["candidate_count"] == 0
    assert result["removed_count"] == 0
    assert link.is_symlink()
    assert outside.read_bytes() == b"synthetic outside target"
