"""Backfill truthful priority/category/area labels on MJobs GitHub issues.

Read-only unless --apply is given. Uses the user's existing gh CLI login and
additive labels; never closes issues or rewrites issue bodies.
"""
from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures
import json
from pathlib import Path
import re
import subprocess
import sys
import time

REPO = "MulattoTech/MJobs"
ROOT = Path(__file__).resolve().parents[1]
PALETTE = {
    **{f"priority:P{i}": (["b60205", "d93f0b", "ed902b", "fbca04", "c5def5", "c2e0c6", "eeeeee"][i], f"Priority P{i}") for i in range(7)},
    **{f"category:{c}": ("dae8fc", f"Issue category: {c}") for c in
       ("bookkeeping", "major-feature", "subfeature", "bug", "documentation",
        "integration", "testing", "operations", "security", "other")},
    "area:untriaged": ("ededed", "Functional area needs triage"),
    "triage:priority-inferred": ("fef2c0", "Default priority; human review suggested"),
}

def gh(path: str, *, method: str | None = None, data: object | None = None) -> object:
    args = ["gh", "api", f"repos/{REPO}/{path}"]
    if method:
        args.extend(["--method", method])
    if data is not None:
        args.extend(["--input", "-"])
    proc = subprocess.run(args, input=None if data is None else json.dumps(data),
                          text=True, encoding="utf-8", capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"GitHub {method or 'GET'} {path}: {proc.stderr.strip()[:350]}")
    return json.loads(proc.stdout)

def paged(path: str) -> list[dict]:
    out = []
    page = 1
    while True:
        part = gh(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        if not isinstance(part, list):
            raise RuntimeError(f"Expected list at {path}")
        out.extend(part)
        if len(part) < 100:
            break
        page += 1
    return out

def seeded() -> dict:
    p = ROOT / "planning/ISSUE_SEED.jsonl"
    return {row["id"]: row for line in p.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() for row in [json.loads(line)]}

def body_field(body: str, label: str) -> str | None:
    match = re.search(rf"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*[`'\" ]*([\w/-]+)", body)
    return match.group(1) if match else None

def labels_for(issue: dict, seed: dict) -> tuple[list[str], bool]:
    names = {v["name"] for v in issue.get("labels", [])}
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    match = re.search(r"MJ-REQ-\d{3}", title + " " + body[:150])
    original = seed.get(match.group(0)) if match else None
    existing_p = [n for n in names if re.fullmatch(r"priority:P[0-6]", n)]
    explicit = body_field(body, "Priority")
    if explicit and not re.fullmatch(r"P[0-6]", explicit, re.I):
        explicit = None
    priority = (existing_p[0].split(":")[-1] if existing_p else
                (original or {}).get("priority") or
                (explicit.upper() if explicit else None))
    inferred = priority is None
    if inferred:
        # Discovery of new sources is P2; otherwise use a reviewable P3 default,
        # not a fabricated high-urgency P0/P1.
        priority = "P2" if issue.get("number") == 517 else "P3"

    current_category = [n for n in names if n.startswith("category:")]
    existing_type = next((n[5:] for n in names if n.startswith("type:")), None)
    if re.search(r"\bMJ-REF-\d+", title) or "child-requirement" in names:
        category = "subfeature"
    elif re.search(r"\bMJ-REQ-\d+", title):
        category = "bookkeeping" if existing_type in ("governance", "tracking", "docs") else "major-feature"
    elif re.search(r"\b(bug|regression|fix)\b", title, re.I) and not re.search(r"\bfeature\b", title, re.I):
        category = "bug"
    elif existing_type in ("governance", "tracking") or re.search(r"\b(audit|tracker|bookkeeping|cleanup)\b", title, re.I):
        category = "bookkeeping"
    elif existing_type == "security":
        category = "security"
    elif existing_type == "test":
        category = "testing"
    elif existing_type == "docs":
        category = "documentation"
    elif existing_type == "integration":
        category = "integration"
    elif existing_type == "ops":
        category = "operations"
    elif existing_type == "frontend" or re.search(r"\b(subfeature|slice|component)\b", title, re.I):
        category = "subfeature"
    elif existing_type == "feature" or re.search(r"\b(feature|implement)\b", title, re.I):
        category = "major-feature"
    else:
        category = "other"

    area = (next((n[5:] for n in names if n.startswith("area:")), None)
            or (original or {}).get("domain")
            or body_field(body, "Area")
            or body_field(body, "Owner")
            or "untriaged")
    additions = {f"priority:{priority}", f"category:{category}", f"area:{area}"}
    if inferred and priority != "P2":
        additions.add("triage:priority-inferred")
    if current_category:
        additions = {n for n in additions if not n.startswith("category:")}
    if existing_p:
        additions = {n for n in additions if not n.startswith("priority:")}
    return sorted(additions - names), inferred

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="For cautious trial runs; 0 means all")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    issues = [v for v in paged("issues?state=all") if "pull_request" not in v]
    issues = sorted(issues, key=lambda x: x["number"])
    if args.limit:
        issues = issues[:args.limit]
    seed = seeded()
    planned = [(i, *labels_for(i, seed)) for i in issues]
    changes = [(i, missing, inferred) for i, missing, inferred in planned if missing]
    counts = Counter(n.split(":")[0] for _, labels, _ in changes for n in labels)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run",
                      "issues_scanned": len(issues), "issues_to_update": len(changes),
                      "new_labels_by_group": dict(counts),
                      "preview": [{"number": a["number"], "add": b} for a, b, _ in changes[:15]]},
                     indent=2), flush=True)
    result = {"updated": [], "errors": []}
    if args.apply:
        defined = {x["name"] for x in paged("labels")}
        needed = {label for _, missing, _ in changes for label in missing}
        for label in sorted(needed - defined):
            color, description = PALETTE.get(label, ("ededed", f"MJobs {label}"))
            gh("labels", method="POST", data={"name": label, "color": color, "description": description})
        # Sequential issue writes are deliberately rate-limited to avoid
        # GitHub's secondary write-rate limit.
        for index, (issue, missing, _) in enumerate(changes, 1):
            try:
                gh(f"issues/{issue['number']}/labels", method="POST", data={"labels": missing})
                result["updated"].append(issue["number"])
            except Exception as exc:
                result["errors"].append({"number": issue["number"], "error": str(exc)})
            if index % 20 == 0:
                print(f"Applied {index}/{len(changes)}; errors={len(result['errors'])}", flush=True)
            time.sleep(0.35)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if result["errors"]:
        print(json.dumps(result["errors"][:12], indent=2), file=sys.stderr)
        return 1
    print(f"Completed: {len(result['updated'])} issues updated.", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
