"""Acceptance census must expose uncertainty instead of manufacturing proof."""
import hashlib

import pytest

from tools.verification import acceptance_integrity as audit


def page(nodes, total, more=False, cursor="one"):
    return {"data": {"user": {"projectV2": {"items": {
        "totalCount": total, "nodes": nodes,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
    }}}}}


def item(number=293, repository="MulattoTech/MJobs"):
    return {"id": str(number), "content": {"number": number,
            "repository": {"nameWithOwner": repository}},
            "fieldValues": {"nodes": [
                {"name": "Done", "field": {"name": "Status"}},
                {"name": "PARTIAL", "field": {"name": "Acceptance"}},
                {"name": "P0", "field": {"name": "Priority"}},
            ], "pageInfo": {"hasNextPage": False}}}


def test_full_pagination_keeps_only_target_repository():
    result = audit.project_items([page([item()], 2, True),
                                  page([item(294, "someone/else")], 2, cursor="two")])
    assert list(result) == [293]


@pytest.mark.parametrize("pages", [
    [page([item()], 2, True)],
    [page([item()], 2)],
    [page([item()], 2, True), page([item()], 2, cursor="two")],
    [page([item()], 1, True), page([], 1, cursor="two")],
])
def test_partial_or_repeated_project_pages_cannot_claim_complete_census(pages):
    with pytest.raises(ValueError):
        audit.project_items(pages)


def test_truncated_project_fields_cannot_silently_hide_acceptance():
    value = item()
    value["fieldValues"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises(ValueError):
        audit.project_items([page([value], 1)])


def test_closed_slice_does_not_complete_partial_parent():
    flags = audit.lifecycle_flags(
        {"status": "PARTIAL", "priority": "P0"},
        {"state": "closed", "state_reason": "completed"},
        {"Status": "Done", "Acceptance": "PARTIAL", "Priority": "P0"})
    assert flags == ["CLOSED_WITHOUT_PASS", "PROJECT_DONE_WITHOUT_PASS"]


def test_historical_hash_drift_and_ci_link_never_become_current_proof():
    old = hashlib.sha256(b"old test").hexdigest()
    evidence = {"source_sha256": {"tests/unit/test_example.py": old},
                "source_candidate": "a" * 40,
                "ci": "https://github.com/MulattoTech/MJobs/actions/runs/123"}
    result = audit.evidence_identity(evidence, {"tests/unit/test_example.py": b"changed test"})
    assert result["source_hashes"] == {"matched": 0, "drifted": 1, "missing": 0}
    assert result["tests"][0]["path"] == "tests/unit/test_example.py"
    assert result["tests"][0]["current_sha256"] != old
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"
    assert result["check_urls"] == [evidence["ci"]]


def test_matching_test_bytes_still_do_not_prove_execution_or_full_scope():
    digest = hashlib.sha256(b"test").hexdigest()
    result = audit.evidence_identity(
        {"files": [{"path": "tests/unit/test_example.py", "sha256": digest}]},
        {"tests/unit/test_example.py": b"test"})
    assert result["source_hashes"]["matched"] == 1
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"


def test_missing_hash_target_remains_missing():
    result = audit.evidence_identity({"source_sha256": {"tests/absent.py": "a" * 64}}, {})
    assert result["source_hashes"]["missing"] == 1


def test_markdown_evidence_is_supported_without_claiming_machine_verified_proof(tmp_path):
    (tmp_path / "evidence.md").write_text("Ran tests/unit/test_example.py; PASS", encoding="utf-8")
    document = audit.read_evidence(tmp_path, "evidence.md")
    result = audit.evidence_identity(document, {"tests/unit/test_example.py": b"test"})
    assert result["tests"][0]["path"] == "tests/unit/test_example.py"
    assert result["proof"] == "HISTORICAL_POINTERS_ONLY"


def test_path_escape_is_not_read_as_evidence(tmp_path):
    with pytest.raises(ValueError):
        audit.read_local_json(tmp_path, "../private.json")


def test_output_cannot_overwrite_canonical_planning(tmp_path):
    planning = tmp_path / "planning"
    planning.mkdir()
    target = planning / "CAPABILITY_MATRIX.json"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="planning"):
        audit.write_report(tmp_path, target, {})
    assert target.read_text(encoding="utf-8") == "original"


def test_lineage_does_not_confuse_parent_with_dotted_child_or_infer_acceptance():
    pulls = [{"number": 677, "title": "MJ-REQ-017.S01 slice", "body": "Refs #293",
              "merged_at": "2026-10-16", "head": {"sha": "a" * 40},
              "merge_commit_sha": "b" * 40, "html_url": "https://github.com/MulattoTech/MJobs/pull/677"}]
    assert audit.pr_lineage("MJ-REQ-017", 18, pulls) == []
    linked = audit.pr_lineage("MJ-REQ-292", 293, pulls)
    assert linked[0]["basis"] == "TEXT_REFERENCE_ONLY"
    assert linked[0]["head_sha"] == "a" * 40


def test_census_covers_all425_without_promoting_historical_results():
    from pathlib import Path
    report = audit.build_census(Path(__file__).resolve().parents[2])
    assert len(report["requirements"]) == 425
    assert sum(report["summary"]["original"].values()) == 366
    assert sum(report["summary"]["supplemental"].values()) == 59
    assert len(report["baseline_p0_ids"]) == 26
    assert {r["fresh_acceptance"] for r in report["requirements"]} == {"NOT_RUN"}
    assert report["requirements"][0]["id"].startswith("MJ-")
