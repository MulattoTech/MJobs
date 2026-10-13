"""Acceptance history: durable evidence, truthful diffs, manual provenance and isolation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.agent_dashboard.acceptance_history import (
    AcceptanceHistory, build_canonical, comparison,
)


def make_project(tmp_path: Path):
    root=tmp_path/"MJobs"
    plan=root/"planning"
    plan.mkdir(parents=True)
    matrix={
        "requirements":[
            {"requirement_id":"MJ-REQ-001","result":"PASS"},
            {"requirement_id":"MJ-REQ-002","result":"PARTIAL"},
            {"requirement_id":"MJ-REQ-003","result":"NOT_RUN"},
            {"requirement_id":"MJ-REQ-004","result":"PASS"},
        ]
    }
    gaps={"canonical_ids":["MJ-REF-001","MJ-REF-002"],
          "result_by_id":{"MJ-REF-001":{"result":"PASS"},"MJ-REF-002":{"result":"NOT_RUN"}},
          "commercial_features":{"MJ-REF-002":{"title":"Safe AI provider fallback"}}}
    (plan/"CAPABILITY_MATRIX.json").write_text(json.dumps(matrix),encoding="utf-8")
    (plan/"REFERENCE_GAP_MATRIX.json").write_text(json.dumps(gaps),encoding="utf-8")
    return root,matrix,gaps


def fake_progress():
    return {
        "available":True,"observed_at":"2026-10-13T18:00:00Z",
        "original":{"pass":2,"partial":1,"not_run":1,"total":4,"accepted_percent":50},
        "reference":{"pass":1,"partial":0,"not_run":1,"total":2,"accepted_percent":50},
        "areas":[
            {"name":"matching_ai","pass":1,"partial":1,"not_run":0,"total":2,"accepted_percent":50},
            {"name":"test_harness","pass":1,"partial":0,"not_run":1,"total":2,"accepted_percent":50}
        ],
        "priorities":[
            {"name":"P0","pass":1,"partial":1,"not_run":0,"total":2,"accepted_percent":50},
            {"name":"P1","pass":1,"partial":0,"not_run":1,"total":2,"accepted_percent":50}
        ],
        "release_readiness":"UNKNOWN",
        "milestone":"TEST-001","original_as_of":"2026-10-13T17:58:00Z",
        "reference_as_of":"2026-10-12T18:00:00Z",
        "git":{"checkout_head_sha":"a"*40,"github_main_sha":"a"*40},
    }


def test_auto_capture_is_durable_and_deduplicated(tmp_path):
    root,_,_=make_project(tmp_path)
    db=tmp_path/"test-acceptance.sqlite3"
    store=AcceptanceHistory(root,db)
    value=fake_progress()
    first=store.observe(value)
    assert store.observe(value)==first
    assert AcceptanceHistory(root,db).get(first)["requirements"]["MJ-REQ-001"]=="PASS"
    assert store.list()["total"]==1
    assert store.get(first)["reference_titles"]["MJ-REF-002"]=="Safe AI provider fallback"
    assert db.is_file()
    assert not (root/"outputs").exists()


def test_same_aggregate_but_different_requirements_is_new_capture(tmp_path):
    root,matrix,_=make_project(tmp_path)
    store=AcceptanceHistory(root,tmp_path/"states.db")
    first=store.observe(fake_progress())
    matrix["requirements"][0]["result"]="NOT_RUN"
    matrix["requirements"][2]["result"]="PASS"
    (root/"planning"/"CAPABILITY_MATRIX.json").write_text(json.dumps(matrix),encoding="utf-8")
    second=store.observe(fake_progress())
    assert second!=first
    diff=comparison(store.get(first),store.get(second))
    assert diff["original"]["delta_pass"]==0
    assert diff["transitions_available"]
    assert [x["id"] for x in diff["status_transitions"]]==["MJ-REQ-001","MJ-REQ-003"]
    assert store.list()["total"]==2


def test_regression_reports_negative_acceptance(tmp_path):
    root,matrix,_=make_project(tmp_path)
    store=AcceptanceHistory(root,tmp_path/"states.db")
    first=store.observe(fake_progress())
    matrix["requirements"][3]["result"]="PARTIAL"
    (root/"planning"/"CAPABILITY_MATRIX.json").write_text(json.dumps(matrix),encoding="utf-8")
    new=fake_progress()
    new["original"].update({"pass":1,"partial":2,"accepted_percent":25})
    new["areas"][1].update({"pass":0,"partial":1,"accepted_percent":0})
    new["priorities"][1].update({"pass":0,"partial":1,"accepted_percent":0})
    second=store.observe(new)
    diff=comparison(store.get(first),store.get(second))
    assert diff["original"]["delta_pass"]==-1
    assert diff["original"]["delta_percent_points"]==-25
    assert next(x for x in diff["areas"] if x["name"]=="test_harness")["delta_pass"]==-1


def test_rejects_mixed_or_invalid_canonical_snapshots(tmp_path):
    root,_,_=make_project(tmp_path)
    mismatched=fake_progress()
    mismatched["original"]["pass"]=3
    with pytest.raises(ValueError,match="matrices changed"):
        build_canonical(root,mismatched)
    bad=fake_progress()
    bad["areas"][1]["name"]="matching_ai"
    with pytest.raises(ValueError,match="duplicated"):
        build_canonical(root,bad)


def test_manual_screenshot_is_attributed_and_lacks_fake_per_id_status(tmp_path):
    root,_,_=make_project(tmp_path)
    store=AcceptanceHistory(root,tmp_path/"states.db")
    screenshot={
        "provenance":"user_screenshot",
        "observed_at":"2026-10-12T15:49:31-05:00",
        "provenance_note":"From user-supplied dashboard image, not proof of acceptance.",
        "original":{"pass":1,"total":4,"partial":None,"not_run":None},
        "reference":{"pass":0,"total":2,"partial":None,"not_run":None},
        "areas":[{"name":"matching_ai","pass":1,"total":2,"partial":None,"not_run":None},
                 {"name":"test_harness","pass":0,"total":2,"partial":None,"not_run":None}],
        "priorities":[{"name":"P0","pass":1,"total":2,"partial":None,"not_run":None},
                      {"name":"P1","pass":0,"total":2,"partial":None,"not_run":None}],
    }
    manual_id=store.import_screenshot(screenshot)
    assert manual_id==store.import_screenshot(screenshot)
    historical=store.get(manual_id)
    assert historical["provenance"]=="user_screenshot"
    assert historical["requirements"]=={}
    assert historical["reference_statuses"]=={}
    now=store.observe(fake_progress())
    difference=comparison(historical,store.get(now))
    assert difference["original"]["delta_pass"]==1
    assert not difference["transitions_available"]


def test_manual_screenshot_must_reconcile_counts(tmp_path):
    root,_,_=make_project(tmp_path)
    bad={
        "provenance":"user_screenshot","observed_at":"2026-10-12T15:49:31-05:00",
        "original":{"pass":2,"total":4},"reference":{"pass":1,"total":2},
        "areas":[{"name":"matching_ai","pass":0,"total":4}],
        "priorities":[{"name":"P0","pass":2,"total":4}]
    }
    with pytest.raises(ValueError,match="area counts"):
        AcceptanceHistory(root,tmp_path/"db.sqlite3").import_screenshot(bad)


def test_parallel_observers_do_not_duplicate_or_corrupt_db(tmp_path):
    root,_,_=make_project(tmp_path)
    store=AcceptanceHistory(root,tmp_path/"parallel.db")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _:store.observe(fake_progress()),range(18)))
    assert len(set(results))==1
    assert store.list()["total"]==1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0]=="ok"


def test_list_sorted_and_bounded(tmp_path):
    root,_,_=make_project(tmp_path)
    store=AcceptanceHistory(root,tmp_path/"bounded.db")
    first=store.observe(fake_progress())
    assert [x["id"] for x in store.list(limit=1)["captures"]]==[first]
    with pytest.raises(ValueError):
        store.list(limit=0)
    with pytest.raises(ValueError):
        store.get(-4)


def test_explicit_checkpoint_captures_zero_change_run(tmp_path):
    root,_,_=make_project(tmp_path)
    store=AcceptanceHistory(root,tmp_path/"manual-checkpoint.db")
    first=store.observe(fake_progress())
    second=store.observe(fake_progress(),force=True)
    assert first!=second
    saved=store.get(second)
    assert saved["capture_kind"]=="manual"
    assert saved["provenance"]=="canonical_local"
    assert saved["requirements"]["MJ-REQ-001"]=="PASS"
    assert store.observe(fake_progress())==first
    assert store.list()["total"]==2
    delta=comparison(store.get(first),saved)
    assert delta["original"]["delta_pass"]==0
    assert delta["reference"]["delta_pass"]==0
