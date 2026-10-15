"""Validate GitHub Project #1 against MJobs issues without changing acceptance evidence.

Usage: python -m tools.verification.project_board_alignment
Requires authenticated gh CLI with access to MulattoTech Project #1.
"""
from __future__ import annotations

import json
import subprocess
import sys


REPOSITORY = "MulattoTech/MJobs"
OWNER = "MulattoTech"
PROJECT_NUMBER = "1"


def github_json(*args: str):
    result = subprocess.run(
        ["gh", *args], text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False, stdin=subprocess.DEVNULL, timeout=90,
    )
    if result.returncode:
        raise RuntimeError("GitHub query failed: " + " ".join(args[:2]))
    if not isinstance(result.stdout, str):
        raise RuntimeError("GitHub returned no decodable JSON")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub returned invalid JSON") from exc


def audit(project: list[dict], issues: list[dict]) -> dict:
    issue_by_number = {i["number"]: i for i in issues}
    project_by_number: dict[int, dict] = {}
    problems = []
    warnings = []

    for card in project:
        content = card.get("content") or {}
        if content.get("type") != "Issue" or content.get("repository") != REPOSITORY:
            problems.append({"kind": "non_repository_issue_card", "title": card.get("title")})
            continue
        number = content.get("number")
        if number in project_by_number:
            problems.append({"kind": "duplicate_project_card", "issue": number})
        project_by_number[number] = card

    for number in sorted(issue_by_number.keys() - project_by_number.keys()):
        problems.append({"kind": "issue_missing_from_project", "issue": number})
    for number in sorted(project_by_number.keys() - issue_by_number.keys()):
        problems.append({"kind": "project_issue_missing_from_repository", "issue": number})

    for number in sorted(issue_by_number.keys() & project_by_number.keys()):
        issue, card = issue_by_number[number], project_by_number[number]
        labels = {label["name"] for label in issue.get("labels", [])}
        priorities = {label.split(":", 1)[1] for label in labels if label.startswith("priority:")}
        if len(priorities) != 1 or card.get("priority") not in priorities:
            problems.append({"kind": "priority_mismatch", "issue": number,
                             "issue_priority_labels": sorted(priorities),
                             "project_priority": card.get("priority")})
        is_closed = issue.get("state", "").upper() == "CLOSED"
        if (card.get("status") == "Done") != is_closed:
            problems.append({"kind": "delivery_status_mismatch", "issue": number,
                             "issue_state": issue.get("state"),
                             "project_status": card.get("status")})
        if card.get("acceptance") == "PASS" and not is_closed:
            warnings.append({"kind": "open_with_pass_acceptance", "issue": number})
        if is_closed and card.get("acceptance") in {"PARTIAL", "FAIL", "BLOCKED", "NOT_RUN"}:
            warnings.append({"kind": "closed_without_pass_acceptance", "issue": number})

    return {"project_cards": len(project), "repository_issues": len(issues),
            "errors": problems, "warnings": warnings, "aligned": not problems}


def main() -> int:
    try:
        project = github_json("project", "item-list", PROJECT_NUMBER, "--owner", OWNER,
                              "--limit", "1000", "--format", "json")["items"]
        issues = github_json("issue", "list", "-R", REPOSITORY, "--state", "all",
                             "--limit", "1000", "--json", "number,state,labels")
        report = audit(project, issues)
    except (KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"PROJECT_BOARD_AUDIT_UNAVAILABLE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["aligned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
