"""Durable, provenance-labeled acceptance snapshots for Mission Control.

This records the canonical status matrix as observed. It never infers acceptance
from commits, a successful CI run, or a screenshot. Storage lives outside Git.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any

VALID_STATUS = {"PASS", "PARTIAL", "NOT_RUN"}
PROVENANCE = {"canonical_local", "user_screenshot"}
DEFAULT_DB = Path(os.environ.get("MJOBS_ACCEPTANCE_HISTORY_DB") or (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "MJobs" / "MissionControl" / "acceptance-history.sqlite3"))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _counts(rows: dict[str, str]) -> dict[str, int]:
    count = Counter(rows.values())
    if set(count) - VALID_STATUS:
        raise ValueError("Unsupported canonical acceptance state")
    return {"pass": count["PASS"], "partial": count["PARTIAL"],
            "not_run": count["NOT_RUN"], "total": len(rows)}


def _normalized_count(value: dict[str, Any]) -> dict[str, Any]:
    values = {k: value.get(k) for k in ("pass", "partial", "not_run", "total")}
    for key, count in values.items():
        if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
            raise ValueError("Invalid acceptance count " + key)
    if values["total"] is None or values["pass"] is None or values["pass"] > values["total"]:
        raise ValueError("Invalid snapshot total")
    if values["partial"] is not None and values["not_run"] is not None:
        if values["pass"] + values["partial"] + values["not_run"] != values["total"]:
            raise ValueError("Snapshot count distribution does not match total")
    values["accepted_percent"] = round(values["pass"] * 100 / values["total"], 1) if values["total"] else 0
    return values


def _group(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    names = set()
    for item in values:
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 90 or name in names:
            raise ValueError("Invalid/duplicated area or priority name")
        names.add(name)
        result.append({"name": name, **_normalized_count(item)})
    return result


def build_canonical(root: Path, live: dict[str, Any]) -> dict[str, Any]:
    """Pin requirement IDs/statuses to the same totals read by the dashboard."""
    if not live.get("available"):
        raise ValueError("Cannot record unavailable acceptance")
    base = Path(root) / "planning"
    matrix = json.loads((base / "CAPABILITY_MATRIX.json").read_text(encoding="utf-8"))
    gaps = json.loads((base / "REFERENCE_GAP_MATRIX.json").read_text(encoding="utf-8"))
    requirements = {r["requirement_id"]: r["result"] for r in matrix["requirements"]}
    references = {
        name: value.get("result") if isinstance(value, dict) else value
        for name, value in gaps["result_by_id"].items()
    }
    if len(requirements) != len(matrix["requirements"]) or len(references) != len(gaps["canonical_ids"]):
        raise ValueError("Duplicate/missing canonical requirement IDs")
    actual_req = _counts(requirements)
    actual_ref = _counts(references)
    for what, expected in (("original", actual_req), ("reference", actual_ref)):
        if any(live[what][key] != expected[key] for key in ("pass", "partial", "not_run", "total")):
            raise ValueError("Acceptance matrices changed while capturing: " + what)
    allowed = {"available", "observed_at", "source", "original", "reference", "areas", "priorities",
               "release_readiness", "milestone", "original_as_of", "reference_as_of", "git",
               "checkpoint_current", "caveat"}
    current = {key: live[key] for key in allowed if key in live}
    current["original"] = _normalized_count(current["original"])
    current["reference"] = _normalized_count(current["reference"])
    current["areas"] = _group(current["areas"])
    current["priorities"] = _group(current["priorities"])
    current["requirements"] = dict(sorted(requirements.items()))
    current["reference_statuses"] = dict(sorted(references.items()))
    titles = gaps.get("commercial_features") or {}
    current["reference_titles"] = {key: str(value.get("title", ""))[:180] for key, value in titles.items()
                                   if isinstance(value, dict) and isinstance(key, str)}
    current["provenance"] = "canonical_local"
    current["capture_kind"] = "automatic"
    current["provenance_note"] = "Observed locally from canonical acceptance matrices; no tests run by history tracker"
    signature = hashlib.sha256(_json({
        "original": current["original"], "reference": current["reference"],
        "areas": current["areas"], "priorities": current["priorities"],
        "requirements": current["requirements"], "references": current["reference_statuses"],
        "release": current.get("release_readiness"),
    }).encode("utf-8")).hexdigest()
    current["fingerprint"] = signature
    return current


def _validated_manual(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("provenance") != "user_screenshot":
        raise ValueError("Historical import requires user_screenshot provenance")
    if not isinstance(record.get("observed_at"), str) or not record["observed_at"]:
        raise ValueError("Historical observation timestamp required")
    try:
        datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid historical timestamp") from exc
    original = _normalized_count(record["original"])
    reference = _normalized_count(record["reference"])
    areas = _group(record["areas"])
    priorities = _group(record["priorities"])
    if sum(x["total"] for x in areas) != original["total"] or sum(x["pass"] for x in areas) != original["pass"]:
        raise ValueError("Screenshot area counts do not reconcile")
    if sum(x["total"] for x in priorities) != original["total"] or sum(x["pass"] for x in priorities) != original["pass"]:
        raise ValueError("Screenshot priority counts do not reconcile")
    return {
        "observed_at": record["observed_at"],
        "source": "USER_SCREENSHOT",
        "provenance": "user_screenshot",
        "capture_kind": "screenshot",
        "provenance_note": str(record.get("provenance_note") or "User-provided historical dashboard image; per-ID statuses unavailable")[:600],
        "original": original, "reference": reference,
        "areas": areas, "priorities": priorities,
        "release_readiness": str(record.get("release_readiness", "UNKNOWN"))[:80],
        "milestone": str(record.get("milestone", "HISTORICAL IMAGE"))[:180],
        "requirements": {}, "reference_statuses": {}, "reference_titles": {},
        "fingerprint": hashlib.sha256(("user_screenshot:" + _json(record)).encode("utf-8")).hexdigest(),
    }


def _delta_record(current: dict[str, Any] | None, prior: dict[str, Any] | None) -> dict[str, Any]:
    if prior is None and current is None:
        return {}
    if current is None:
        return {"name": prior["name"], "before": prior, "after": None, "delta_pass": None, "delta_total": None}
    if prior is None:
        return {"name": current["name"], "before": None, "after": current, "delta_pass": None, "delta_total": None}
    return {
        "name": current["name"], "before": prior, "after": current,
        "delta_pass": current["pass"] - prior["pass"],
        "delta_total": current["total"] - prior["total"],
        "delta_percent_points": round(current["accepted_percent"] - prior["accepted_percent"], 1),
    }


def comparison(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result = {
        "baseline_id": before.get("id"), "current_id": after.get("id"),
        "baseline_at": before["observed_at"], "current_at": after["observed_at"],
        "baseline_provenance": before["provenance"], "current_provenance": after["provenance"],
        "original": _delta_record({"name":"original",**after["original"]},{"name":"original",**before["original"]}),
        "reference": _delta_record({"name":"reference",**after["reference"]},{"name":"reference",**before["reference"]}),
        "areas": [], "priorities": [], "status_transitions": [],
        "transitions_available": bool(before.get("requirements") and after.get("requirements")),
    }
    for key in ("areas", "priorities"):
        prior = {entry["name"]: entry for entry in before[key]}
        current = {entry["name"]: entry for entry in after[key]}
        result[key] = [
            _delta_record(current.get(name), prior.get(name))
            for name in sorted(set(prior)|set(current))
        ]
    if result["transitions_available"]:
        for category, status_key in (("original", "requirements"), ("reference", "reference_statuses")):
            prior = before.get(status_key) or {}
            current = after.get(status_key) or {}
            for req in sorted(set(prior) & set(current)):
                if prior[req] != current[req]:
                    result["status_transitions"].append({
                        "catalogue": category, "id": req,
                        "before": prior[req], "after": current[req],
                    })
    return result


class AcceptanceHistory:
    def __init__(self, root: Path, db_path: Path | None = None):
        self.root = Path(root)
        self.db_path = Path(db_path or DEFAULT_DB)
        self._lock = threading.RLock()

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=8)
        db.execute("PRAGMA busy_timeout=8000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signature TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            provenance TEXT NOT NULL,
            document TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_capture_time ON captures(observed_at,id)")
        return db

    def _record(self, document: dict[str, Any]) -> int:
        if document.get("provenance") not in PROVENANCE:
            raise ValueError("Invalid capture provenance")
        signature = document["fingerprint"]
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "INSERT OR IGNORE INTO captures(signature,observed_at,stored_at,provenance,document) VALUES (?,?,?,?,?)",
                (signature, document["observed_at"], _utc_now(), document["provenance"], _json(document)),
            )
            db.commit()
            item = db.execute("SELECT id FROM captures WHERE signature=?", (signature,)).fetchone()
            return int(item[0])

    def observe(self, live: dict[str, Any], *, force: bool = False) -> int:
        document = build_canonical(self.root, live)
        if force:
            document["observed_at"] = _utc_now()
            document["capture_kind"] = "manual"
            document["provenance_note"] = "User-requested time checkpoint, recorded from canonical matrices. No tests run by history tracker."
            document["fingerprint"] = hashlib.sha256((document["fingerprint"] + "|manual|" + uuid.uuid4().hex).encode("utf-8")).hexdigest()
        return self._record(document)

    def import_screenshot(self, record: dict[str, Any]) -> int:
        return self._record(_validated_manual(record))

    def _read(self, db: sqlite3.Connection, row) -> dict[str, Any]:
        result = json.loads(row[1])
        result["id"] = row[0]
        return result

    def get(self, capture_id: int) -> dict[str, Any] | None:
        if capture_id < 1:
            raise ValueError("Capture ID must be positive")
        with self._lock, closing(self._connect()) as db:
            row = db.execute("SELECT id,document FROM captures WHERE id=?", (capture_id,)).fetchone()
            return self._read(db,row) if row else None

    def list(self, limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("Invalid timeline limit")
        with self._lock, closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id,document FROM captures ORDER BY observed_at DESC,id DESC LIMIT ?", (limit,)
            ).fetchall()
            total = db.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        captures = []
        for cap_id, raw in reversed(rows):
            data = json.loads(raw)
            captures.append({
                "id": cap_id, "observed_at": data["observed_at"],
                "provenance": data["provenance"], "capture_kind": data.get("capture_kind", "automatic"), "provenance_note": data.get("provenance_note"),
                "original": data["original"], "reference": data["reference"],
                "areas": data["areas"], "priorities": data["priorities"],
                "release_readiness": data.get("release_readiness", "UNKNOWN"),
                "milestone": data.get("milestone"), "git": data.get("git", {}),
            })
        return {"captures": captures, "total": total, "limit": limit, "storage": "local-sqlite",
                "note": "Only observed canonical states and separately marked screenshot imports are historical; commit timestamps alone never certify acceptance."}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Import a user-provided historical acceptance screenshot into the local history DB.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--screenshot-json", type=Path, required=True)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()
    data = json.loads(args.screenshot_json.read_text(encoding="utf-8"))
    store = AcceptanceHistory(args.root, args.db)
    identifier = store.import_screenshot(data)
    print("IMPORTED_HISTORICAL_SCREENSHOT_ID", identifier, "DB", store.db_path)
if __name__ == "__main__":
    main()
