"""Acceptance census must expose uncertainty instead of manufacturing proof."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

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


@pytest.fixture
def disposable_git_worktree(tmp_path):
    """All administrative-path reproductions stay inside this disposable tree."""
    repository, worktree = tmp_path / "repository", tmp_path / "worktree"
    def git(*args, cwd=repository):
        return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE).decode().strip()
    repository.mkdir()
    git("init")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "fixture")
    git("worktree", "add", "-b", "fixture-worktree", str(worktree))
    git_dir = Path(git("rev-parse", "--absolute-git-dir", cwd=worktree)).resolve()
    common_dir = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree)).resolve()
    assert git_dir.is_relative_to(tmp_path.resolve())
    assert common_dir.is_relative_to(tmp_path.resolve())
    return repository, worktree, git_dir, common_dir


@pytest.mark.parametrize("destination", ["marker", "git_dir", "common_dir", "ordinary_repo", "new_admin_file"])
def test_output_rejects_git_administration_before_write(disposable_git_worktree, destination):
    repository, worktree, git_dir, common_dir = disposable_git_worktree
    root = repository if destination == "ordinary_repo" else worktree
    target = {"marker": worktree / ".git", "git_dir": git_dir / "HEAD",
              "common_dir": common_dir / "config", "ordinary_repo": common_dir / "HEAD",
              "new_admin_file": git_dir / "unexpected-report.json"}[destination]
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(root, target, {"unsafe": True})
    assert (target.read_bytes() if target.exists() else None) == before
    subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True)


@pytest.mark.parametrize("destination", ["marker", "git_dir", "common_dir"])
def test_output_rejects_symlink_alias_to_git_metadata(disposable_git_worktree, tmp_path, destination):
    _, worktree, git_dir, common_dir = disposable_git_worktree
    target = {"marker": worktree / ".git", "git_dir": git_dir, "common_dir": common_dir}[destination]
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    output = alias if destination == "marker" else alias / "HEAD"
    before = output.read_bytes()
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(worktree, output, {})
    assert output.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive filesystem alias")
def test_output_rejects_windows_case_alias(disposable_git_worktree):
    _, worktree, _, _ = disposable_git_worktree
    marker = worktree / ".GiT"
    before = marker.read_bytes()
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(worktree, marker, {})
    assert marker.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
@pytest.mark.parametrize("destination", ["git_dir", "common_dir"])
def test_output_rejects_windows_junction_alias(disposable_git_worktree, tmp_path, destination):
    _, worktree, git_dir, common_dir = disposable_git_worktree
    target = git_dir if destination == "git_dir" else common_dir
    alias = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                   check=True, capture_output=True)
    output = alias / "HEAD"
    before = output.read_bytes()
    with pytest.raises(ValueError, match="Git administrative"):
        audit.write_report(worktree, output, {})
    assert output.read_bytes() == before


def test_output_allows_external_artifact_and_untracked_workspace_report(disposable_git_worktree, tmp_path):
    _, worktree, _, _ = disposable_git_worktree
    artifact_directory = tmp_path / "ao-artifacts"
    artifact_directory.mkdir()
    for target in (artifact_directory / "report.json", worktree / "report.json"):
        audit.write_report(worktree, target, {"count": 425})
        assert json.loads(target.read_text(encoding="utf-8")) == {"count": 425}


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
