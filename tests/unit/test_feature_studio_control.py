"""Feature Studio persistent control plane and REST security smoke tests."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tools.feature_studio.control import StudioStore, plan, probe
from tools.feature_studio.validate import STUDIO

FIXTURE = json.loads((STUDIO / "specs" / "FS-TEST-001.json").read_text(encoding="utf-8"))


def test_storage_revision_idempotency_and_audit(tmp_path):
    store=StudioStore(tmp_path / "studio.db")
    first=store.save(copy.deepcopy(FIXTURE),expected_revision=0,key="request-12345678")
    assert first["revision"]==1
    assert store.save(copy.deepcopy(FIXTURE),expected_revision=0,key="request-12345678")["replayed"]
    assert len(store.events())==1
    with pytest.raises(ValueError,match="Revision conflict"):
        store.save(copy.deepcopy(FIXTURE),expected_revision=0,key="request-xyz98765")
    assert store.get("MulattoTech/MJobs","FS-TEST-001")["spec"]["lifecycle"]=="DRAFT"
    changed=copy.deepcopy(FIXTURE)
    changed["problem"]="A revised test of the feature studio"
    next_version=store.save(changed,expected_revision=1,key="request-revision-2")
    assert next_version["revision"]==2
    assert len(store.events())==2


def test_replay_with_changed_body_rejected(tmp_path):
    store=StudioStore(tmp_path/"studio.db")
    store.save(copy.deepcopy(FIXTURE),expected_revision=0,key="my-key-12345678")
    altered=copy.deepcopy(FIXTURE)
    altered["title"]="Conflicting update"
    with pytest.raises(ValueError,match="reused"):
        store.save(altered,expected_revision=0,key="my-key-12345678")


def test_approval_and_agent_execution_cannot_be_imported(tmp_path):
    store=StudioStore(tmp_path/"studio.db")
    for field,value in [("spec_review_approved",True),("agent_execution_authorized",True)]:
        altered=copy.deepcopy(FIXTURE)
        altered["execution"][field]=value
        with pytest.raises(ValueError):
            store.save(altered,expected_revision=0,key="deny-"+field)


def test_plan_always_requires_review():
    findings={"candidates":[{"path":"apps/web/src/App.tsx","score":1},{"path":"apps/api/server.py","score":2}]}
    result=plan(FIXTURE,findings)
    assert not result["execution_enabled"]
    assert result["approval_required"]
    assert len(result["workstreams"])==2


def test_probe_is_grounded_in_tracked_paths(tmp_path):
    import subprocess
    subprocess.run(["git","init","-q",str(tmp_path)],check=True)
    (tmp_path/"job-matching.py").write_text("pass",encoding="utf-8")
    subprocess.run(["git","-C",str(tmp_path),"add","job-matching.py"],check=True)
    spec=copy.deepcopy(FIXTURE)
    spec["title"]="Job matching"
    result=probe(tmp_path,spec)
    assert result["tracked_files"]==1
    assert all(x["path"]=="job-matching.py" for x in result["candidates"])
    assert not result["execution_started"]


def test_dashboard_studio_endpoints_refuse_unauthenticated_writes(tmp_path, monkeypatch):
    from tools.feature_studio import control
    monkeypatch.setattr(control,"DEFAULT_DB",tmp_path/"studio.db")
    from tools.agent_dashboard.server import create_app
    with TestClient(create_app(Path(__file__).resolve().parents[2],mission_instance=True)) as client:
        health=client.get("/api/feature-studio/health")
        assert health.status_code==200
        assert health.json()["agent_execution"] is False
        payload={"spec":FIXTURE,"expected_revision":0,"idempotency_key":"my-test-12345678"}
        unauth=client.post("/api/feature-studio/specs",json=payload)
        assert unauth.status_code==403
        options=client.get("/api/launcher/options").json()
        authenticated=client.post("/api/feature-studio/specs",json=payload,headers={"X-MJobs-Launch-Token":options["token"]})
        assert authenticated.status_code==200,authenticated.text
        assert authenticated.json()["revision"]==1
        repeat=client.post("/api/feature-studio/specs",json=payload,headers={"X-MJobs-Launch-Token":options["token"]})
        assert repeat.json()["replayed"] is True
        assert len(client.get("/api/feature-studio/events").json()["events"])==1
