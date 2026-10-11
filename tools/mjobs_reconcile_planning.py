"""Build compact, backwards-compatible MJobs issue metadata from source catalogues."""
from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "planning"

def load(name: str) -> dict:
    return json.loads((PLAN / name).read_text(encoding="utf-8-sig"))

def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def compile_index() -> dict:
    seeds = [json.loads(line) for line in (PLAN / "ISSUE_SEED.jsonl").read_text(
        encoding="utf-8-sig").splitlines() if line.strip()]
    matrix = load("CAPABILITY_MATRIX.json")
    issue_map = load("GITHUB_ISSUE_MAP.json")
    reference = load("REFERENCE_GAP_MATRIX.json")
    ref_issue_map = load("REFERENCE_GAP_ISSUE_MAP.json")
    by_id = {entry["requirement_id"]: entry for entry in matrix["requirements"]}
    if len(seeds) != len(by_id) or set(by_id) != {s["id"] for s in seeds}:
        raise ValueError("Seed/catalogue identifier coverage mismatch")
    if set(issue_map["issues"]) != set(by_id):
        raise ValueError("Original issue map coverage mismatch")

    rows = []
    meta = {}
    for seed in seeds:
        rid = seed["id"]
        area, priority = seed["domain"], seed["priority"]
        if priority not in {f"P{i}" for i in range(7)} or not area:
            raise ValueError(f"Invalid priority/area: {rid}")
        category = ("bookkeeping" if any(
            label in seed.get("labels", []) for label in ("type:governance", "type:tracking", "type:docs")
        ) else "major-feature")
        by_id[rid]["priority"] = priority
        by_id[rid]["area"] = area
        by_id[rid]["category"] = category
        record = {"id": rid, "issue": issue_map["issues"][rid],
                  "priority": priority, "area": area, "category": category,
                  "status": by_id[rid]["result"]}
        rows.append(record)
        meta[rid] = {"priority": priority, "area": area, "category": category}
    for rid in reference["canonical_ids"]:
        feature = reference.get("commercial_features", {}).get(rid, {})
        rows.append({
            "id": rid,
            "issue": ref_issue_map["issues"][rid],
            "priority": feature.get("priority", "P3"),
            "area": feature.get("owner_area", "untriaged"),
            "category": "subfeature",
            "status": reference["result_by_id"][rid]["result"],
            **({"triage_required": True} if not feature.get("priority") else {}),
        })

    rows.sort(key=lambda item: (item["id"].split("-")[1], item["id"]))
    counts = Counter(item["status"] for item in rows)
    index = {"schema_version": 1, "source": "ISSUE_SEED + CAPABILITY_MATRIX + REFERENCE_GAP_MATRIX",
             "requirements": rows, "summary": dict(counts),
             "note": "Acceptance statuses are evidence-based. Branches/commits are not proof of PASS."}
    return {"matrix": matrix, "map": issue_map, "metadata": meta, "index": index}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    output = compile_index()
    encoded = json.dumps(output["index"], ensure_ascii=False).encode("utf-8")
    print(f"Indexed {len(output['index']['requirements'])} requirements; "
          f"compact index {len(encoded):,} bytes")
    if len(encoded) > 100_000:
        raise ValueError("Compact requirement index exceeds 100 KB")
    if args.apply:
        output["map"]["metadata"] = output["metadata"]
        # Keep the original scalar issue lookup intact for existing consumers.
        save(PLAN / "CAPABILITY_MATRIX.json", output["matrix"])
        save(PLAN / "GITHUB_ISSUE_MAP.json", output["map"])
        save(PLAN / "REQUIREMENT_INDEX.json", output["index"])
        labels = load("LABELS.json")
        existing = {row["name"] for row in labels}
        for category in ("bookkeeping", "major-feature", "subfeature", "bug",
                         "documentation", "integration", "testing", "operations",
                         "security", "other"):
            name = f"category:{category}"
            if name not in existing:
                labels.append({"name": name, "description": f"MJobs issue category: {category}"})
                existing.add(name)
        for label in ("area:untriaged", "triage:priority-inferred"):
            if label not in existing:
                labels.append({"name": label, "description": "Classification requires human triage"})
        save(PLAN / "LABELS.json", labels)
        print("Updated metadata, compact index, and label definitions.")

if __name__ == "__main__":
    main()
