"""Source-aware Mission Control Git history regression tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tools.agent_dashboard.github_history import parse_main_commits
from tools.agent_dashboard.work_history import WorkHistory
from tools.agent_dashboard.server import create_app


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git","-C",str(root),*args],
                          text=True,capture_output=True,encoding="utf-8")
    assert proc.returncode == 0,proc.stderr
    return proc.stdout.strip()


def repo(tmp_path: Path) -> Path:
    root=tmp_path/"MJobs"
    root.mkdir()
    git(root,"init")
    git(root,"checkout","-b","main")
    git(root,"config","user.name","Fixture")
    git(root,"config","user.email","test@example.invalid")
    (root/"AGENTS.md").write_text("# Test\n",encoding="utf-8")
    git(root,"add",".")
    git(root,"commit","-m","docs: baseline")
    git(root,"checkout","-b","feature/other-work")
    folder=root/"apps"/"web"/"src"
    folder.mkdir(parents=True)
    (folder/"feature.tsx").write_text("export const feature=true\n",encoding="utf-8")
    git(root,"add",".")
    git(root,"commit","-m","feat: other-branch implementation")
    git(root,"checkout","main")
    return root


def fake_github_rows():
    return [{
        "sha":"a"*40,
        "html_url":"https://github.com/MulattoTech/MJobs/commit/"+"a"*40,
        "commit":{
            "message":"Merge pull request #611: reviewed feature",
            "author":{"name":"Fixture Author","date":"2026-10-12T20:19:18Z"},
            "committer":{"name":"Fixture Merger","date":"2026-10-12T20:19:25Z"},
        },
    }]


def test_other_local_branch_is_visible_without_checkout(tmp_path):
    root=repo(tmp_path)
    initial=git(root,"rev-parse","HEAD")
    history=WorkHistory(root,codex_home=tmp_path/"no-codex")
    with patch("tools.agent_dashboard.work_history.session_activity",
               return_value={"available":False}):
        checkout=history.snapshot(source="checkout")
        across=history.snapshot(source="all")
        cached=history.snapshot(source="all",limit=1)
    assert checkout["history_source"]=="checkout"
    assert checkout["head"]==initial
    assert across["history_source"]=="all"
    assert across["commits"][0]["subject"]=="feat: other-branch implementation"
    assert across["head"]!=initial
    assert "unpushed" in across["source_description"]
    assert len(cached["commits"])==1
    assert git(root,"rev-parse","HEAD")==initial
    assert git(root,"status","--porcelain")==""
    assert not (root/"outputs").exists()


def test_github_main_is_separate_metadata_without_fake_line_counts(tmp_path):
    root=repo(tmp_path)
    history=WorkHistory(root,codex_home=tmp_path/"no-codex")
    rows=parse_main_commits(fake_github_rows())
    assert rows[0]["files_changed"] is None
    assert rows[0]["category"]=="OTHER"
    assert rows[0]["published"] is True
    assert rows[0]["issue_refs"]==["#611"]
    with patch("tools.agent_dashboard.work_history.fetch_github_main",return_value=rows) as gh, \
         patch("tools.agent_dashboard.work_history.session_activity",
               return_value={"available":False}):
        first=history.snapshot(source="github")
        second=history.snapshot(source="github",limit=1)
    assert gh.call_count==1
    assert first["source"]=="GITHUB_MAIN_PUBLISHED"
    assert first["code_commits"] is None
    assert first["change_total"] is None
    assert first["classification_available"] is False
    assert second["commits"][0]["short_sha"]=="a"*10


def test_github_parser_ignores_invalid_rows():
    rows=parse_main_commits([{"sha":"invalid","commit":{"message":"bad"}}, {}, *fake_github_rows()])
    assert len(rows)==1
    with pytest.raises(ValueError):
        parse_main_commits({"message":"not a list"})


def test_source_api_validation_and_no_agent_launch(tmp_path):
    root=repo(tmp_path)
    from tools.agent_dashboard.model_catalog import ModelCatalog, _choice
    with patch.object(ModelCatalog,"_discover",return_value=(
        [_choice("gpt-6-luna","Luna",["high"],"high")],"fixture"
    )):
        app=create_app(root,mission_instance=True)
    client=TestClient(app)
    with patch("tools.agent_dashboard.work_history.session_activity",
               return_value={"available":False}), \
         patch.object(app.state.supervisor,"start",side_effect=AssertionError("Agent launched")):
        all_data=client.get("/api/history?source=all").json()
        assert all_data["commits"][0]["subject"]=="feat: other-branch implementation"
        assert client.get("/api/history?source=unsupported").status_code==422
