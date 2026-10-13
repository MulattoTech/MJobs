"""Canonical acceptance history records: immutable snapshots and truthful signed deltas."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import shutil
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from tools.agent_dashboard.progress_history import ProgressHistory, current_capture, compare, state_fingerprint
from tools.agent_dashboard.server import create_app

ROOT=Path(__file__).resolve().parents[2]

def test_history_capture_source_and_totals():
    data=current_capture(ROOT)
    assert data["original"]["total"]==366
    assert data["reference"]["total"]==59
    assert len(data["requirements"])==366
    assert len(data["references"])==59
    assert sum(data["original"][k] for k in ("pass","partial","not_run"))==366

def test_append_only_dedupe_manual_and_signed_changes(tmp_path):
    root=tmp_path/"workspace"
    root.mkdir()
    data=current_capture(ROOT)
    db=ProgressHistory(root,db_path=tmp_path/"history.db")
    revised=copy.deepcopy(data)
    names=sorted(revised["requirements"])
    first=next(k for k in names if revised["requirements"][k]["status"]=="NOT_RUN")
    revised["requirements"][first]["status"]="PASS"
    area=revised["requirements"][first]["area"]
    pri=revised["requirements"][first]["priority"]
    for group,label in [("areas",area),("priorities",pri)]:
        revised[group][label]["pass"]+=1
        revised[group][label]["not_run"]-=1
    revised["original"]["pass"]+=1
    revised["original"]["not_run"]-=1
    with patch("tools.agent_dashboard.progress_history.current_capture",side_effect=[data,data,revised,revised,data]):
        a=db.observe()
        same=db.observe()
        b=db.observe()
        manual=db.observe(force=True)
        regress=db.observe()
    assert a["created"] and not same["created"]
    assert b["created"] and manual["reason"]=="manual"
    assert regress["created"]
    points=db.timeline()["points"]
    assert [x["id"] for x in points]==[a["snapshot_id"],b["snapshot_id"],manual["snapshot_id"],regress["snapshot_id"]]
    forward=db.difference(a["snapshot_id"],b["snapshot_id"])["delta"]
    assert forward["original_pass"]==1
    assert forward["areas"][area]["pass"]==1
    assert forward["transitions"]["requirements"][0]["id"]==first
    backward=db.difference(b["snapshot_id"],regress["snapshot_id"])["delta"]
    assert backward["original_pass"]==-1
    assert backward["transitions"]["requirements"][0]["after"]=="NOT_RUN"
    assert db.detail(manual["snapshot_id"])["reason"]=="manual"

def test_history_different_roots_do_not_share_db(tmp_path):
    a=ProgressHistory(tmp_path/"one")
    b=ProgressHistory(tmp_path/"two")
    assert a.db_path != b.db_path

def test_history_does_not_mutate_source_matrices(tmp_path):
    fake=tmp_path/"project"
    (fake/"planning").mkdir(parents=True)
    for name in ["CAPABILITY_MATRIX.json","REFERENCE_GAP_MATRIX.json","IMPLEMENTATION_STATE.json","ISSUE_SEED.jsonl"]:
        shutil.copyfile(ROOT/"planning"/name,fake/"planning"/name)
    before={p.name:p.read_bytes() for p in (fake/"planning").iterdir()}
    db=ProgressHistory(fake,db_path=tmp_path/"isolated.db")
    db.observe()
    db.observe()
    assert all(p.read_bytes()==before[p.name] for p in (fake/"planning").iterdir())

def test_missing_history_id_and_rejected_limit(tmp_path):
    h=ProgressHistory(ROOT,tmp_path/"history.db")
    with pytest.raises(KeyError):
        h.detail(12345)
    with pytest.raises(ValueError):
        h.timeline(limit=0)

def test_progress_history_api_routes_and_token(tmp_path):
    from tools.agent_dashboard.model_catalog import ModelCatalog,_choice
    with patch.object(ModelCatalog,"_discover",return_value=([_choice("gpt-6-luna","Luna",["high"],"high")],"fixture")):
        app=create_app(ROOT,mission_instance=True,progress_history_db=tmp_path/"route.db")
    with TestClient(app) as client:
        unauthorized=client.post("/api/progress/history/capture")
        assert unauthorized.status_code==403
        token=app.state.mission_control.token
        capture=client.post("/api/progress/history/capture",headers={"X-MJobs-Launch-Token":token})
        assert capture.status_code==200,capture.text
        timeline=client.get("/api/progress/history")
        assert timeline.status_code==200
        sid=capture.json()["snapshot_id"]
        assert client.get(f"/api/progress/history/{sid}").status_code==200
        assert client.get("/api/progress/history/12345678").status_code==404
        assert client.get("/api/progress/history?limit=0").status_code==422
