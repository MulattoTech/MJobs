"""Reconciliation must not silently erase contradictory acceptance records."""
import json
from pathlib import Path
import shutil

import pytest

from tools import mjobs_reconcile_planning as reconcile


@pytest.fixture
def planning(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "planning"
    for name in ("ISSUE_SEED.jsonl", "CAPABILITY_MATRIX.json", "GITHUB_ISSUE_MAP.json",
                 "REFERENCE_GAP_MATRIX.json", "REFERENCE_GAP_ISSUE_MAP.json"):
        shutil.copyfile(source / name, tmp_path / name)
    monkeypatch.setattr(reconcile, "PLAN", tmp_path)
    return tmp_path


def change(planning, name, mutate):
    path = planning / name
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_duplicate_original_acceptance_cannot_overwrite_result(planning):
    def duplicate(data):
        data["requirements"].append({**data["requirements"][0], "result": "PASS"})
    change(planning, "CAPABILITY_MATRIX.json", duplicate)
    with pytest.raises(ValueError, match="Duplicate.*MJ-REQ"):
        reconcile.compile_index()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_reference_denominator_cannot_silently_change(planning, mutation):
    def mutate(data):
        if mutation == "missing":
            data["canonical_ids"].pop()
        elif mutation == "duplicate":
            data["canonical_ids"].append(data["canonical_ids"][0])
        else:
            data["result_by_id"]["MJ-REF-060"] = {"result": "PASS", "evidence": ["x"]}
    change(planning, "REFERENCE_GAP_MATRIX.json", mutate)
    with pytest.raises(ValueError, match="Reference.*coverage|Duplicate.*MJ-REF"):
        reconcile.compile_index()


def test_unknown_acceptance_state_is_rejected(planning):
    change(planning, "CAPABILITY_MATRIX.json",
           lambda d: d["requirements"][0].update(result="DONE"))
    with pytest.raises(ValueError, match="Invalid.*result"):
        reconcile.compile_index()


def test_valid_generation_keeps_separate_catalogues_and_is_read_only(planning):
    before = {p.name: p.read_bytes() for p in planning.iterdir()}
    rows = reconcile.compile_index()["index"]["requirements"]
    assert sum(r["id"].startswith("MJ-REQ-") for r in rows) == 366
    assert sum(r["id"].startswith("MJ-REF-") for r in rows) == 59
    assert {p.name: p.read_bytes() for p in planning.iterdir()} == before


def test_apply_rejects_conflict_before_any_canonical_write(planning, monkeypatch):
    change(planning, "CAPABILITY_MATRIX.json",
           lambda d: d["requirements"].append(dict(d["requirements"][0])))
    before = {p.name: p.read_bytes() for p in planning.iterdir()}
    monkeypatch.setattr("sys.argv", ["reconcile", "--apply"])
    with pytest.raises(ValueError, match="Duplicate"):
        reconcile.main()
    assert {p.name: p.read_bytes() for p in planning.iterdir()} == before
