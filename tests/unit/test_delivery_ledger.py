"""Read-only delivery history: real local Git fixtures and synthetic GitHub data."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from tools.agent_dashboard import delivery_ledger as ledger
from tools.agent_dashboard.progress_history import ProgressHistory


def run(*args, cwd=None):
    proc = subprocess.run(
        list(map(str, args)), cwd=cwd, check=True,
        capture_output=True, encoding="utf8", errors="replace",
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    project = tmp_path / "MJobs"
    project.mkdir()
    run("git", "init", "-q", "-b", "main", project)
    run("git", "-C", project, "config", "user.name", "Fixture")
    run("git", "-C", project, "config", "user.email", "fixture@example.invalid")
    (project / "planning").mkdir()
    return project


def commit(project, message):
    run("git", "-C", project, "add", "-A")
    run("git", "-C", project, "commit", "-q", "-m", message)
    sha = run("git", "-C", project, "rev-parse", "HEAD")
    run("git", "-C", project, "update-ref", "refs/remotes/origin/main", sha)
    return sha


def put_matrices(project, *, req_status, ref_status="PARTIAL"):
    original = {
        "schema_version": 1,
        "requirements": [
            {"requirement_id": "MJ-REQ-277", "result": req_status, "priority": "P1"},
            {"requirement_id": "MJ-REQ-014", "result": "PARTIAL", "priority": "P0"},
        ],
    }
    refs = {
        "canonical_ids": ["MJ-REF-001"],
        "result_by_id": {"MJ-REF-001": ref_status},
    }
    (project / "planning" / "CAPABILITY_MATRIX.json").write_text(json.dumps(original))
    (project / "planning" / "REFERENCE_GAP_MATRIX.json").write_text(json.dumps(refs))


def test_status_map_preserves_authoritative_ids_and_results():
    assert ledger._status_map({"requirements": [
        {"requirement_id": "MJ-REQ-001", "result": "PASS"},
        {"requirement_id": "MJ-REQ-002", "result": "unverified"},
    ]}, "original") == {"MJ-REQ-001": "PASS"}
    assert ledger._status_map({"result_by_id": {
        "MJ-REF-001": {"result": "PASS"}, "MJ-REF-002": "PARTIAL",
    }}, "reference") == {"MJ-REF-001": "PASS", "MJ-REF-002": "PARTIAL"}
    assert ledger._status_map(None, "original") == {}


def test_git_matrix_history_proves_real_main_promotion_and_not_baseline(repo):
    put_matrices(repo, req_status="PARTIAL")
    commit(repo, "baseline acceptance")
    put_matrices(repo, req_status="PASS", ref_status="PASS")
    promotion = commit(repo, "accept MJ-REQ-277")
    revisions = ledger._git_matrix_commits(repo, "planning/CAPABILITY_MATRIX.json")
    assert len(revisions) == 2
    objects = ledger._batch_matrix(repo, revisions, "planning/CAPABILITY_MATRIX.json")
    assert objects[0]["requirements"][0]["result"] == "PASS"
    assert objects[1]["requirements"][0]["result"] == "PARTIAL"
    events, coverage = ledger._matrix_events(repo, {"MJ-REQ-277": 278})
    assert len(events) == 2
    req = next(event for event in events if event["requirement_id"] == "MJ-REQ-277")
    assert req["before"] == "PARTIAL" and req["after"] == "PASS"
    assert req["verified_pass"] is True and req["sha"] == promotion
    assert req["url"] == "https://github.com/MulattoTech/MJobs/issues/278"
    assert coverage["original"]["compared"] == 1


def test_first_observation_is_not_falsely_declared_as_promotion(repo, tmp_path):
    history = ProgressHistory(repo, db_path=tmp_path / "history.sqlite3")
    capture = {
        "requirements": {"MJ-REQ-014": {"status": "PASS"}},
        "references": {"MJ-REF-001": {"status": "PASS"}},
    }
    with history._db() as db:
        db.execute(
            "INSERT INTO observations (captured_at,fingerprint,data,reason) VALUES(?,?,?,?)",
            ("2026-10-14T12:00:00Z", "finger1", json.dumps(capture), "baseline"),
        )
    assert ledger._captured_transitions(history, {}) == []


def test_capture_transition_marks_only_first_observed_time(repo, tmp_path):
    history = ProgressHistory(repo, db_path=tmp_path / "history.sqlite3")
    prior = {
        "requirements": {"MJ-REQ-277": {"status": "PARTIAL"}},
        "references": {"MJ-REF-001": {"status": "NOT_RUN"}},
    }
    current = {
        "requirements": {"MJ-REQ-277": {"status": "PASS"}},
        "references": {"MJ-REF-001": {"status": "PARTIAL"}},
    }
    with history._db() as db:
        for i, capture in enumerate([prior, current], 1):
            db.execute(
                "INSERT INTO observations (captured_at,fingerprint,data,reason) VALUES(?,?,?,?)",
                (f"2026-10-14T12:0{i}:00Z", f"finger{i}", json.dumps(capture),
                 "baseline" if i == 1 else "changed"),
            )
    events = ledger._captured_transitions(history, {"MJ-REQ-277": 278})
    assert len(events) == 2
    passed = next(event for event in events if event["requirement_id"] == "MJ-REQ-277")
    assert passed["verified_pass"] is True
    assert passed["evidence"].startswith("First observed in capture #")
    assert passed["url"].endswith("/issues/278")
    assert passed["at"].startswith("2026-10-14T12:02")


def test_github_history_records_open_merge_and_closure_without_faking_pass(monkeypatch):
    def pages(path):
        if path.startswith("pulls?"):
            return [
                {"number": 650, "title": "Safety slice", "created_at": "2026-10-13T10:00:00Z",
                 "merged_at": "2026-10-14T10:00:00Z", "closed_at": "2026-10-14T10:00:00Z"},
                {"number": 651, "title": "Unmerged", "created_at": "2026-10-14T12:00:00Z",
                 "closed_at": "2026-10-14T15:00:00Z", "merged_at": None},
            ]
        if path.startswith("issues?"):
            return [
                {"number": 278, "title": "Settings", "created_at": "2026-10-12T12:00:00Z",
                 "closed_at": "2026-10-14T10:00:00Z"},
                {"number": 650, "pull_request": {}, "created_at": "2026-10-13T10:00:00Z"},
            ]
        return [{"name": "fix/branch", "commit": {"sha": "a" * 40}}]
    monkeypatch.setattr(ledger, "_gh_pages", pages)
    events, sources = ledger._github_events()
    assert len([x for x in events if x["kind"] == "pr"]) == 4
    assert len([x for x in events if x["kind"] == "issue"]) == 2
    merged = next(x for x in events if x["id"] == "pr:merged:650")
    assert merged["status"] == "merged"
    assert not merged.get("verified_pass")
    assert sources["branches"]["heads"][0]["name"] == "fix/branch"


def test_commit_deduplicates_local_branches_and_includes_unique_clone_history(repo):
    (repo / "README.md").write_text("base")
    base = commit(repo, "first")
    run("git", "-C", repo, "branch", "feature-one")
    clones = repo / ".mjobs-recovery" / "clones" / "another"
    clones.parent.mkdir(parents=True)
    run("git", "clone", "-q", "--local", repo, clones)
    run("git", "-C", clones, "config", "user.name", "Fixture")
    run("git", "-C", clones, "config", "user.email", "fixture@example.invalid")
    (clones / "UNIQUE.md").write_text("unpublished")
    run("git", "-C", clones, "add", "UNIQUE.md")
    run("git", "-C", clones, "commit", "-q", "-m", "unique local recovery")
    events, coverage = ledger._all_git_events(repo)
    assert len(events) == 2
    assert coverage["unique_commits"] == 2
    original = next(row for row in events if row["sha"] == base)
    assert original["published_on_cached_main"] is True
    assert len(original["scopes"]) == 2
    unique = next(row for row in events if row["sha"] != base)
    assert unique["published_on_cached_main"] is False and unique["url"] is None


def test_archived_dirty_work_is_in_inventory_not_falsely_new_delivery_event(repo):
    (repo / "README.md").write_text("main")
    commit(repo, "baseline")
    active = repo / ".mjobs-recovery" / "active" / "round3"
    archived = repo / ".mjobs-recovery" / "clones" / "historic"
    for folder in [active, archived]:
        folder.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", "-q", repo, folder)
        (folder / "new.py").write_text("pending work")
    items, events, coverage = ledger._workspace_inventory(repo)
    assert coverage["registered_and_independent"] >= 3
    assert coverage["archived_dirty_workspaces"] == 1
    assert len([e for e in events if e["kind"] == "workspace"]) == 1
    assert events[0]["workspace"] == str(active)
    assert any(row["workspace"] == str(archived) and row["state"] == "DIRTY"
               for row in items)


def test_paginated_filters_cover_all_types_without_misreporting(monkeypatch, repo, tmp_path):
    history = ProgressHistory(repo, db_path=tmp_path / "history.sqlite3")
    event1 = ledger._event("commit", "commit:abc", "2026-10-13T00:00:00Z",
                           "startup implementation", "Git history")
    event2 = ledger._event("acceptance", "matrix:123:MJ-REQ-014",
                           "2026-10-14T00:00:00Z", "MJ-REQ-014: PARTIAL -> PASS",
                           "Git main", requirement_id="MJ-REQ-014", verified_pass=True)
    store = ledger.DeliveryLedger(repo, history)
    calls = []
    def fake():
        calls.append(1)
        return {"events": [event2, event1], "inventory": [],
                "coverage": {}, "observed_at": "2026-10-14T13:00:00Z", "note": "sample"}
    monkeypatch.setattr(store, "_read", fake)
    assert store.query(limit=1)["events"][0]["id"] == event2["id"]
    assert store.query(offset=1, limit=1)["events"][0]["id"] == event1["id"]
    assert store.query(kind="pass")["total_matching"] == 1
    assert store.query(kind="commit", search="startup")["total_matching"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize("kwargs", [
    {"kind": "unauthorized"}, {"limit": 151}, {"offset": -1},
    {"search": "x" * 121},
])
def test_invalid_query_fails_before_read(monkeypatch, repo, tmp_path, kwargs):
    history = ProgressHistory(repo, db_path=tmp_path / "history.sqlite3")
    store = ledger.DeliveryLedger(repo, history)
    monkeypatch.setattr(store, "_read", Mock(side_effect=AssertionError("source touched")))
    with pytest.raises(ValueError):
        store.query(**kwargs)


def test_api_route_is_readonly_and_enforces_bounds(monkeypatch, repo, tmp_path):
    from tools.agent_dashboard import server
    monkeypatch.setattr(
        ledger.DeliveryLedger, "_read",
        lambda self: {"events": [ledger._event(
            "commit", "commit:abc", "2026-10-14T12:00:00Z",
            "task", "Git history")], "inventory": [], "coverage": {},
            "observed_at": "2026-10-14T13:00:00Z", "note": "fixture"},
    )
    app = server.create_app(repo, progress_history_db=tmp_path / "history.sqlite3")
    with TestClient(app) as client:
        response = client.get("/api/delivery-ledger?limit=1")
        assert response.status_code == 200
        assert response.json()["events"][0]["kind"] == "commit"
        assert client.get("/api/delivery-ledger?kind=invalid").status_code == 422
        assert client.get("/api/delivery-ledger?limit=151").status_code == 422

def test_remote_git_branch_commits_are_read_without_local_fetch(monkeypatch):
    calls = []
    remote_sha = "b" * 40
    def fake_run(command, *, timeout=25):
        calls.append(command)
        assert command[:2] == ["gh", "api"]
        assert "compare/main..." in command[-1]
        return json.dumps({
            "ahead_by": 1,
            "commits": [{
                "sha": remote_sha,
                "commit": {
                    "message": "Implement MJ-REQ-014",
                    "author": {"name": "Codex", "date": "2026-10-14T20:00:00Z"},
                    "committer": {"date": "2026-10-14T20:00:00Z"},
                },
            }],
        })
    monkeypatch.setattr(ledger, "_run", fake_run)
    events, coverage = ledger._remote_branch_events([
        {"name": "fix/feature-startup", "sha": remote_sha},
    ])
    assert coverage["inspected"] == 1
    assert coverage["additional_commit_rows"] == 1
    assert coverage["branch_head_events"] == 1
    assert not coverage["truncated"]
    commit_row = next(e for e in events if e["kind"] == "commit")
    assert commit_row["sha"] == remote_sha
    assert "MJ-REQ-014" in commit_row["requirement_ids"]
    assert commit_row["scopes"] == ["GitHub branch: fix/feature-startup"]
    branch_row = next(e for e in events if e["kind"] == "branch")
    assert branch_row["branch"] == "fix/feature-startup"
    assert len(calls) == 1


def test_remote_branch_comparison_failures_surface_as_partial_coverage(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError("synthetic inaccessible branch")
    monkeypatch.setattr(ledger, "_run", blocked)
    events, coverage = ledger._remote_branch_events([
        {"name": "fix/inaccessible", "sha": "a" * 40},
        {"name": "bad..path", "sha": "b" * 40},
    ])
    assert events == []
    assert coverage["inspected"] == 2
    assert coverage["truncated"] is True
    assert len(coverage["errors"]) == 2


def test_remote_branch_api_does_not_return_unverified_pass_state(monkeypatch):
    monkeypatch.setattr(ledger, "_run", lambda *args, **kwargs: json.dumps({
        "ahead_by": 200,
        "commits": [{
            "sha": "f" * 40,
            "commit": {"message": "Fix MJ-REQ-277",
                       "committer": {"date": "2026-10-14T11:00:00Z"}},
        }],
    }))
    events, coverage = ledger._remote_branch_events([
        {"name": "feat/pending", "sha": "f" * 40},
    ])
    assert coverage["truncated"] is True
    assert not any(e.get("verified_pass") for e in events)
    assert coverage["errors"][0]["ahead_by"] == 200


def test_branch_kind_is_valid_pagination_filter(monkeypatch, repo, tmp_path):
    history = ProgressHistory(repo, db_path=tmp_path / "history.sqlite3")
    store = ledger.DeliveryLedger(repo, history)
    row = ledger._event(
        "branch", "branch-head:feature", "2026-10-14T18:00:00Z",
        "Branch HEAD", "GitHub branch head",
    )
    monkeypatch.setattr(store, "_read", lambda: {
        "events": [row], "inventory": [], "coverage": {},
        "observed_at": "2026-10-14T18:00:00Z", "note": "",
    })
    assert store.query(kind="branch")["total_matching"] == 1
