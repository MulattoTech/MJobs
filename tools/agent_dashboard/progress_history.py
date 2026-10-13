"""Append-only, content-verified Mission Control acceptance snapshots.

Observations live in per-user LOCALAPPDATA, not tracked Git. No acceptance
state is changed and neither GitHub nor a model is contacted.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

ALLOWED = {"PASS", "PARTIAL", "NOT_RUN"}
DEFAULT_DB = Path(os.getenv("LOCALAPPDATA") or Path.home()/".local"/"share") / "MJobs" / "MissionControl" / "acceptance-history.sqlite3"
MAX_POINTS = 500


def current_capture(root: Path) -> dict[str, Any]:
    """Read the two authoritative acceptance matrices with full item identity."""
    root = Path(root)
    from tools.verification.progress_dashboard import snapshot
    summary = snapshot(root)  # validates catalogue IDs and allowed statuses
    original = json.loads((root/"planning/CAPABILITY_MATRIX.json").read_text(encoding="utf-8"))
    reference = json.loads((root/"planning/REFERENCE_GAP_MATRIX.json").read_text(encoding="utf-8"))
    seeds = [json.loads(line) for line in (root/"planning/ISSUE_SEED.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    metadata = {row["id"]: row for row in seeds}
    requirements = {}
    for row in original["requirements"]:
        rid = row["requirement_id"]
        if rid not in metadata:
            raise ValueError("Unknown requirement ID")
        seed = metadata[rid]
        requirements[rid] = {
            "id": rid, "status": row["result"],
            "area": seed["domain"], "priority": seed["priority"],
            "issue": seed.get("issue_number") or seed.get("issue"),
        }
    refs = {
        rid: {"id": rid, "status": value.get("result") if isinstance(value,dict) else value}
        for rid,value in reference["result_by_id"].items()
    }
    if set(refs) != set(reference["canonical_ids"]):
        raise ValueError("Reference ID mismatch")
    if len(requirements) != summary["original_total"] or len(refs) != summary["reference_total"]:
        raise ValueError("Unexpected catalogue count")
    if any(x["status"] not in ALLOWED for x in list(requirements.values()) + list(refs.values())):
        raise ValueError("Unknown acceptance result")
    def totals(items):
        result = Counter(x["status"] for x in items)
        return {"pass":result["PASS"],"partial":result["PARTIAL"],"not_run":result["NOT_RUN"],"total":len(items)}
    groups={"areas":{},"priorities":{}}
    for kind,field in [("areas","area"),("priorities","priority")]:
        by=defaultdict(list)
        for item in requirements.values():
            by[item[field]].append(item)
        groups[kind]={key:totals(rows) for key,rows in sorted(by.items())}
    return {
        "source": "LOCAL_CANONICAL_MATRICES",
        "original_as_of": summary["original_as_of"],
        "reference_as_of": summary["reference_as_of"],
        "release_readiness": summary["release"],
        "milestone": summary["milestone"],
        "original": totals(list(requirements.values())),
        "reference": totals(list(refs.values())),
        "areas":groups["areas"],"priorities":groups["priorities"],
        "requirements":requirements,"references":refs,
    }


def state_fingerprint(data: dict[str,Any]) -> str:
    # The content identity excludes observation time and Git checkout branch.
    canonical={
        "requirements": {k:v["status"] for k,v in data["requirements"].items()},
        "references": {k:v["status"] for k,v in data["references"].items()},
        "release_readiness":data["release_readiness"],
        "milestone":data["milestone"],
    }
    return hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def compare(current: dict[str,Any], previous: dict[str,Any]) -> dict[str,Any]:
    """Deltas and individual item transitions; never call them completed features."""
    groups={}
    for group in ["areas","priorities"]:
        names=set(current[group])|set(previous[group])
        groups[group]={name:{
            "pass":current[group].get(name,{}).get("pass",0)-previous[group].get(name,{}).get("pass",0),
            "partial":current[group].get(name,{}).get("partial",0)-previous[group].get(name,{}).get("partial",0),
            "not_run":current[group].get(name,{}).get("not_run",0)-previous[group].get(name,{}).get("not_run",0),
            "total":current[group].get(name,{}).get("total",0)-previous[group].get(name,{}).get("total",0),
        } for name in sorted(names)}
    transitions={"requirements":[],"references":[]}
    for field in transitions:
        before=previous[field]
        after=current[field]
        for rid in sorted(set(before)|set(after)):
            a=before.get(rid)
            b=after.get(rid)
            old=a["status"] if a else "ABSENT"
            new=b["status"] if b else "ABSENT"
            if old!=new:
                item=b or a
                transitions[field].append({
                    "id":rid,"before":old,"after":new,
                    "area":item.get("area") if item else None,
                    "priority":item.get("priority") if item else None,
                })
    return {
        "original_pass":current["original"]["pass"]-previous["original"]["pass"],
        "reference_pass":current["reference"]["pass"]-previous["reference"]["pass"],
        "areas":groups["areas"],"priorities":groups["priorities"],
        "transitions":transitions,
        "changed_count":len(transitions["requirements"])+len(transitions["references"]),
    }


class ProgressHistory:
    def __init__(self, root:Path, db_path:Path|None=None):
        self.root=Path(root).resolve()
        root_key=hashlib.sha256(str(self.root).lower().encode("utf-8")).hexdigest()[:16]
        self.db_path=Path(db_path or DEFAULT_DB.with_name("acceptance-"+root_key+".sqlite3"))
        self._lock=threading.RLock()
        self.db_path.parent.mkdir(parents=True,exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                data TEXT NOT NULL,
                reason TEXT NOT NULL CHECK(reason IN ('changed','manual','baseline'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_observations_time ON observations(captured_at,id)")

    def _db(self):
        return sqlite3.connect(self.db_path,timeout=8)

    def _latest(self,db):
        record=db.execute("SELECT id,captured_at,fingerprint,data,reason FROM observations ORDER BY id DESC LIMIT 1").fetchone()
        if not record:return None
        return {"id":record[0],"captured_at":record[1],"fingerprint":record[2],"data":json.loads(record[3]),"reason":record[4]}

    def observe(self,*,force:bool=False,reason:str="changed")->dict[str,Any]:
        data=current_capture(self.root)
        digest=state_fingerprint(data)
        with self._lock,self._db() as db:
            previous=self._latest(db)
            if previous and previous["fingerprint"]==digest and not force:
                return {"created":False,"snapshot_id":previous["id"],"captured_at":previous["captured_at"],
                        "fingerprint":digest}
            why=("manual" if force else "changed" if previous else "baseline")
            stamp=datetime.now(timezone.utc).isoformat()
            cursor=db.execute("INSERT INTO observations(captured_at,fingerprint,data,reason) VALUES(?,?,?,?)",
                              (stamp,digest,json.dumps(data,separators=(",",":"),sort_keys=True),why))
            return {"created":True,"snapshot_id":cursor.lastrowid,"captured_at":stamp,
                    "fingerprint":digest,"reason":why}

    def timeline(self,limit:int=120)->dict[str,Any]:
        if limit<1 or limit>MAX_POINTS:raise ValueError("Invalid history limit")
        with self._lock,self._db() as db:
            rows=db.execute("SELECT id,captured_at,reason,data FROM observations ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
            first=db.execute("SELECT captured_at FROM observations ORDER BY id ASC LIMIT 1").fetchone()
        points=[]
        for snapshot_id,stamp,reason,data_json in reversed(rows):
            d=json.loads(data_json)
            points.append({
                "id":snapshot_id,"at":stamp,"reason":reason,"original":d["original"],"reference":d["reference"],
                "areas":d["areas"],"priorities":d["priorities"],"release_readiness":d["release_readiness"],
                "original_as_of":d["original_as_of"],"reference_as_of":d["reference_as_of"],
            })
        return {"points":points,"total_returned":len(points),
                "first_capture":first[0] if first else None,
                "note":"Only captured observations are shown. No invented historical screenshots or backdated acceptance."}

    def detail(self,snapshot_id:int)->dict[str,Any]:
        with self._lock,self._db() as db:
            row=db.execute("SELECT id,captured_at,reason,data FROM observations WHERE id=?",(snapshot_id,)).fetchone()
        if not row:raise KeyError("Unknown progress snapshot")
        return {"id":row[0],"at":row[1],"reason":row[2],"data":json.loads(row[3])}

    def difference(self,older_id:int,newer_id:int)->dict[str,Any]:
        older=self.detail(older_id)
        newer=self.detail(newer_id)
        return {"before":{"id":older["id"],"at":older["at"]},
                "after":{"id":newer["id"],"at":newer["at"]},
                "delta":compare(newer["data"],older["data"])}
