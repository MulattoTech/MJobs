"""Read-only acceptance census. Pointers and matching bytes are never test passes.

Use --github to collect public issue/Project metadata, or --snapshot to replay a
previous capture. Output is an adjudication aid; this module never changes the
canonical matrices or provider state and never executes evidence commands.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPOSITORY = "MulattoTech/MJobs"
PROJECT_QUERY = '''query($endCursor:String){user(login:"MulattoTech"){
projectV2(number:1){items(first:100,after:$endCursor){totalCount
pageInfo{hasNextPage endCursor} nodes{id content{... on Issue{number
repository{nameWithOwner}}} fieldValues(first:100){nodes{
... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2SingleSelectField{name}}}
... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2Field{name}}}
} pageInfo{hasNextPage}}}}}}}'''
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TEST_PATH = re.compile(r"\btests/[\w./-]+\.py(?:\:[\w:.[\]-]+)?")
PUBLIC_URL = re.compile(r"https://github\.com/MulattoTech/MJobs/(?:actions/[^\s\"<>]+|pull/\d+)")
REQUIREMENT = re.compile(r"\bMJ-(?:REQ|REF)-\d+(?:\.[A-Z]\d+)?\b")


def read_local_json(root: Path, relative: str):
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Evidence path escapes repository")
    return json.loads(target.read_text(encoding="utf-8-sig"))


def read_evidence(root: Path, relative: str) -> dict:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Evidence path escapes repository")
    if target.suffix.lower() == ".md":
        return {"text": target.read_text(encoding="utf-8-sig")}
    return read_local_json(root, relative)


def project_items(pages: list[dict]) -> dict[int, dict]:
    """Reject incomplete or inconsistent snapshots, including nested truncation."""
    if not pages:
        raise ValueError("Missing Project pages")
    seen, cursors, result = set(), set(), {}
    total = None
    for index, page in enumerate(pages):
        if page.get("errors"):
            raise ValueError("GraphQL reported errors")
        connection = page["data"]["user"]["projectV2"]["items"]
        if total is None:
            total = connection["totalCount"]
        if total != connection["totalCount"]:
            raise ValueError("Project changed during pagination")
        info = connection["pageInfo"]
        if info["hasNextPage"] != (index < len(pages) - 1):
            raise ValueError("Incomplete Project pagination")
        if info["endCursor"] in cursors or (not connection["nodes"] and total):
            raise ValueError("Repeated or empty Project page")
        cursors.add(info["endCursor"])
        for item in connection["nodes"]:
            if item["id"] in seen:
                raise ValueError("Duplicate Project item")
            seen.add(item["id"])
            if item["fieldValues"]["pageInfo"]["hasNextPage"]:
                raise ValueError("Incomplete Project field pagination")
            content = item.get("content") or {}
            if content.get("repository", {}).get("nameWithOwner") != REPOSITORY:
                continue
            number = content["number"]
            if number in result:
                raise ValueError("Duplicate issue in Project")
            result[number] = {v["field"]["name"]: v.get("name", v.get("text"))
                              for v in item["fieldValues"]["nodes"] if v.get("field")}
    if len(seen) != total:
        raise ValueError("Project count does not match fetched items")
    return result


def lifecycle_flags(row: dict, issue: dict | None, project: dict | None) -> list[str]:
    flags = []
    if issue is None:
        flags.append("ISSUE_MISSING")
    elif issue.get("state", "").upper() == "CLOSED" and row["status"] != "PASS":
        flags.append("CLOSED_WITHOUT_PASS")
    if project is None:
        flags.append("PROJECT_ITEM_MISSING")
    else:
        if project.get("Status") == "Done" and row["status"] != "PASS":
            flags.append("PROJECT_DONE_WITHOUT_PASS")
        if project.get("Acceptance") != row["status"]:
            flags.append("PROJECT_ACCEPTANCE_DRIFT")
        if project.get("Priority") != row["priority"]:
            flags.append("PROJECT_PRIORITY_DRIFT")
    return flags


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def evidence_identity(document: dict, sources: dict[str, bytes]) -> dict:
    """Extract recorded identities without treating arbitrary prose as execution."""
    hashes, tests, revisions, urls = set(), set(), set(), set()
    for value in _walk(document):
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and SHA256.fullmatch(str(value.get("sha256", ""))):
                hashes.add((value["path"], value["sha256"]))
            for key, child in value.items():
                if isinstance(child, str) and "/" in key and SHA256.fullmatch(child):
                    hashes.add((key, child))
                if isinstance(child, str) and re.fullmatch(r"[0-9a-f]{40}", child):
                    if any(word in key.lower() for word in ("commit", "revision", "head", "candidate", "base", "sha")) and "tree" not in key.lower():
                        revisions.add(child)
        elif isinstance(value, str):
            tests.update(TEST_PATH.findall(value.replace("\\", "/")))
            tests.update(m.group(0).replace(".", "/") + ".py"
                         for m in re.finditer(r"\btests\.(?:unit|integration|browser)\.test_\w+", value))
            urls.update(PUBLIC_URL.findall(value))
    counts = Counter({"matched": 0, "drifted": 0, "missing": 0})
    drifted = []
    for path, expected in sorted(hashes):
        actual = hashlib.sha256(sources[path]).hexdigest() if path in sources else None
        state = "missing" if actual is None else "matched" if actual == expected else "drifted"
        counts[state] += 1
        if state != "matched":
            drifted.append(path)
        if path.startswith("tests/"):
            tests.add(path)
    return {
        "proof": "HISTORICAL_POINTERS_ONLY", "revisions": sorted(revisions),
        "source_hashes": dict(counts), "changed_or_missing_paths": sorted(set(drifted)),
        "tests": [{"path": test, "recorded_sha256": sorted(digest for path, digest in hashes
                                                           if path == test.split('::')[0]),
                   "current_sha256": hashlib.sha256(sources[test.split('::')[0]]).hexdigest()
                   if test.split('::')[0] in sources else None} for test in sorted(tests)],
        "check_urls": sorted(url for url in urls if "/actions/" in url),
        "pr_urls": sorted(url for url in urls if "/pull/" in url),
    }


def _run(args: list[str], root: Path | None = None) -> bytes:
    return subprocess.run(args, cwd=root, check=True, capture_output=True).stdout


def git_sources(root: Path, revision: str) -> dict[str, bytes]:
    """Read Git blob bytes, avoiding Windows checkout newline conversions."""
    tree = _run(["git", "ls-tree", "-rz", revision], root)
    entries = {}
    for line in tree.split(b"\0"):
        if line:
            meta, path = line.split(b"\t", 1)
            mode, kind, oid = meta.split()
            if kind == b"blob":
                entries[path.decode("utf-8")] = oid
    oids = list(dict.fromkeys(entries.values()))
    response = subprocess.run(["git", "cat-file", "--batch"], cwd=root,
                              input=b"\n".join(oids) + b"\n", capture_output=True, check=True).stdout
    blobs, offset = {}, 0
    for oid in oids:
        end = response.index(b"\n", offset)
        size = int(response[offset:end].split()[-1])
        blobs[oid] = response[end + 1:end + 1 + size]
        offset = end + size + 2
    return {path: blobs[oid] for path, oid in entries.items()}


def capture_provider() -> dict:
    pages = json.loads(_run(["gh", "api", "graphql", "--paginate", "--slurp", "-f", "query=" + PROJECT_QUERY]))
    project_items(pages)
    issue_pages = json.loads(_run(["gh", "api", "--paginate", "--slurp",
                                  f"repos/{REPOSITORY}/issues?state=all&per_page=100"]))
    issues = [issue for page in issue_pages for issue in page if "pull_request" not in issue]
    pull_pages = json.loads(_run(["gh", "api", "--paginate", "--slurp",
                                 f"repos/{REPOSITORY}/pulls?state=all&per_page=100"]))
    return {"captured_at": datetime.now(timezone.utc).isoformat(), "project_pages": pages,
            "issues": issues, "pulls": [pull for page in pull_pages for pull in page]}


def pr_lineage(identifier: str, issue_number: int, pulls: list[dict]) -> list[dict]:
    """Text correlation is discoverability, never scope or acceptance proof."""
    result = []
    for pull in pulls:
        text = pull.get("title", "") + "\n" + (pull.get("body") or "")
        if identifier in REQUIREMENT.findall(text) or re.search(rf"(?<!\w)#{issue_number}(?!\d)", text):
            result.append({"number": pull["number"], "url": pull["html_url"],
                           "head_sha": pull["head"]["sha"], "merge_sha": pull.get("merge_commit_sha"),
                           "merged_at": pull.get("merged_at"), "basis": "TEXT_REFERENCE_ONLY"})
    return result


def commit_lineage(root: Path, revision: str) -> dict[str, list[str]]:
    raw = _run(["git", "log", revision, "--format=%H%x00%B%x00"], root).decode("utf-8")
    parts = raw.split("\0")
    result = {}
    for index in range(0, len(parts) - 1, 2):
        commit, body = parts[index].strip(), parts[index + 1]
        for identifier in set(REQUIREMENT.findall(body)):
            result.setdefault(identifier, []).append(commit)
    return result


def build_census(root: Path, snapshot: dict | None = None) -> dict:
    revision = _run(["git", "rev-parse", "HEAD"], root).decode().strip()
    sources = git_sources(root, revision)
    commits = commit_lineage(root, revision)
    matrix = read_local_json(root, "planning/CAPABILITY_MATRIX.json")
    reference = read_local_json(root, "planning/REFERENCE_GAP_MATRIX.json")
    index = read_local_json(root, "planning/REQUIREMENT_INDEX.json")
    original_map = read_local_json(root, "planning/GITHUB_ISSUE_MAP.json")["issues"]
    reference_map = read_local_json(root, "planning/REFERENCE_GAP_ISSUE_MAP.json")["issues"]
    seeds = [json.loads(line) for line in (root / "planning/ISSUE_SEED.jsonl").read_text(
        encoding="utf-8-sig").splitlines() if line.strip()]
    seed_by_id = {s["id"]: s for s in seeds}
    canonical = {r["requirement_id"]: r for r in matrix["requirements"]}
    if (len(seeds) != 366 or len(seed_by_id) != 366 or len(matrix["requirements"]) != 366
            or set(canonical) != set(seed_by_id)):
        raise ValueError("Original census must cover exactly 366 distinct seed requirements")
    expected_ref = {f"MJ-REF-{n:03d}" for n in range(1, 60)}
    if (len(reference["canonical_ids"]) != 59 or set(reference["canonical_ids"]) != expected_ref
            or set(reference["result_by_id"]) != expected_ref):
        raise ValueError("Supplemental census must cover exactly 59 distinct requirements")
    if set(original_map) != set(canonical) or set(reference_map) != expected_ref:
        raise ValueError("Issue map coverage mismatch")
    canonical.update(reference["result_by_id"])
    issue_map = {**original_map, **reference_map}
    index_by_id = {r["id"]: r for r in index["requirements"]}
    if len(index["requirements"]) != 425 or set(index_by_id) != set(canonical):
        raise ValueError("Compact index coverage mismatch")
    projects = project_items(snapshot["project_pages"]) if snapshot else {}
    issues = {i["number"]: i for i in snapshot["issues"]} if snapshot else {}
    if snapshot and len(issues) != len(snapshot["issues"]):
        raise ValueError("Duplicate issue in snapshot")
    evidence, rows = {}, []
    for rid, entry in sorted(canonical.items()):
        seed = seed_by_id.get(rid)
        feature = reference.get("commercial_features", {}).get(rid, {})
        issue = issues.get(issue_map[rid])
        project = projects.get(issue_map[rid])
        priority = seed["priority"] if seed else feature.get("priority", "P3")
        row = {"id": rid, "issue": issue_map[rid], "priority": priority,
               "status": entry["result"], "evidence": entry.get("evidence", []),
               "proposed_state": entry["result"], "fresh_acceptance": "NOT_RUN"}
        row["priority_basis"] = "ISSUE_SEED" if seed else "COMMERCIAL_CONTRACT" if feature.get("priority") else "INFERRED_DEFAULT_REQUIRES_TRIAGE"
        row["implementation_lineage"] = {
            "basis": "TEXT_REFERENCE_ONLY_NOT_ACCEPTANCE", "commits": commits.get(rid, []),
            "pulls": pr_lineage(rid, issue_map[rid], snapshot.get("pulls", [])) if snapshot else [],
        }
        flags = lifecycle_flags(row, issue, project) if snapshot else ["PROVIDER_NOT_QUERIED"]
        if row["priority_basis"] == "INFERRED_DEFAULT_REQUIRES_TRIAGE" and "PROJECT_PRIORITY_DRIFT" in flags:
            flags.remove("PROJECT_PRIORITY_DRIFT")
            flags.append("INFERRED_PRIORITY_DIFFERS_FROM_PROJECT")
        indexed = index_by_id[rid]
        if any(indexed.get(key) != row[key] for key in ("status", "priority", "issue")):
            flags.append("INDEX_DRIFT")
        contract = seed["body"] if seed else (issue or {}).get("body", "")
        row["contract"] = {"sha256": hashlib.sha256(contract.encode()).hexdigest() if contract else None,
                           "source": "ISSUE_SEED" if seed else "GITHUB_ISSUE",
                           "source_lines": seed.get("source_lines") if seed else None,
                           "spec": f"docs/spec/areas/{seed['domain']}.md" if seed else None}
        if not contract:
            flags.append("CONTRACT_NOT_CAPTURED")
        if issue:
            row["issue_state"] = {k: issue.get(k) for k in ("state", "state_reason", "closed_at")}
            if seed and contract.replace("\r\n", "\n").strip() != issue.get("body", "").replace("\r\n", "\n").strip():
                flags.append("ISSUE_CONTRACT_DIFFERS")
            row["issue_labels"] = sorted(label["name"] for label in issue.get("labels", [])
                                         if label["name"].startswith(("acceptance:", "priority:")))
        row["project"] = {key: project.get(key) for key in ("Status", "Acceptance", "Priority")} if project else None
        for path in row["evidence"]:
            if path not in evidence:
                try:
                    document = read_evidence(root, path)
                    evidence[path] = {"record_sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                                      **evidence_identity(document, sources)}
                except (OSError, ValueError):
                    evidence[path] = {"proof": "MISSING_OR_INVALID_RECORD"}
            if evidence[path]["proof"] == "MISSING_OR_INVALID_RECORD":
                flags.append("EVIDENCE_MISSING_OR_INVALID")
        if row["status"] == "PASS" and not row["evidence"]:
            flags.append("PASS_WITHOUT_EVIDENCE")
            row["proposed_state"] = "NOT_RUN"
        if row["evidence"]:
            if any(evidence[p].get("source_hashes", {}).get("drifted") for p in row["evidence"]):
                flags.append("HISTORICAL_SOURCE_DRIFT")
            if not any(evidence[p].get("tests") for p in row["evidence"]):
                flags.append("TEST_IDENTITY_NOT_EXTRACTED")
        row["flags"] = sorted(set(flags))
        row["uncertainty"] = "Full contract not re-executed; historical drift is not disproof. Root adjudication required."
        rows.append(row)
    return {
        "schema_version": 1, "source_revision": revision,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider_captured_at": snapshot.get("captured_at") if snapshot else None,
        "input_sha256": {path: hashlib.sha256((root / path).read_bytes()).hexdigest()
                         for path in ("planning/CAPABILITY_MATRIX.json", "planning/REFERENCE_GAP_MATRIX.json",
                                      "planning/REQUIREMENT_INDEX.json", "planning/ISSUE_SEED.jsonl",
                                      "planning/GITHUB_ISSUE_MAP.json", "planning/REFERENCE_GAP_ISSUE_MAP.json")},
        "summary": {"original": dict(Counter(r["status"] for r in rows if r["id"].startswith("MJ-REQ"))),
                    "supplemental": dict(Counter(r["status"] for r in rows if r["id"].startswith("MJ-REF"))),
                    "p0": dict(Counter(r["status"] for r in rows if r["priority"] == "P0" and r["id"].startswith("MJ-REQ"))),
                    "flags": dict(Counter(f for r in rows for f in r["flags"]))},
        "policy": "Canonical recorded results only. No new acceptance PASS; no provider mutations. Source hashes use HEAD Git blobs. CI links are unverified historical pointers, not current checks.",
        "baseline_p0_ids": [r["id"] for r in rows if r["priority"] == "P0" and r["id"].startswith("MJ-REQ")],
        "requirements": rows, "evidence_index": evidence,
    }


def write_report(root: Path, output: Path, report: dict) -> None:
    """An explicit output destination cannot turn the census into a source edit."""
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root / "planning"):
        raise ValueError("Census output must not overwrite canonical planning")
    # Linked worktrees have an untracked .git pointer, while their actual
    # administrative directories live outside the checkout. Resolve aliases
    # before comparing; WindowsPath also preserves Windows case semantics.
    if output.is_relative_to((root / ".git").resolve()):
        raise ValueError("Census output must not overwrite Git administrative paths")
    admin_paths = _run(["git", "rev-parse", "--path-format=absolute",
                       "--git-dir", "--git-common-dir"], root).decode("utf-8").splitlines()
    if len(admin_paths) != 2 or any(not path for path in admin_paths):
        raise ValueError("Could not identify Git administrative paths")
    if any(output.is_relative_to(Path(path).resolve()) for path in admin_paths):
        raise ValueError("Census output must not overwrite Git administrative paths")
    if output.is_relative_to(root):
        tracked = _run(["git", "ls-files", "--", output.relative_to(root).as_posix()], root)
        if tracked.strip():
            raise ValueError("Census output must not overwrite a tracked file")
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--github", action="store_true")
    group.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = capture_provider() if args.github else json.loads(args.snapshot.read_text(encoding="utf-8-sig")) if args.snapshot else None
    report = build_census(args.root, snapshot)
    write_report(args.root, args.output, report)
    print(json.dumps(report["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
