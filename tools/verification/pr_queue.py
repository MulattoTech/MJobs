"""Read-only MJobs publication preflight. Never publishes, merges or cancels CI.

Snapshot input is GitHub REST /pulls data, not gh's display-oriented author shape.
Offline snapshots and reviewed metadata are trusted operator inputs, not signed proofs.

Existing-PR updates require --live --candidate FILE --metadata FILE --update-pr N.
The candidate additionally names previous_head_sha, base_repo and base_ref. The
separate operator review for N binds head_sha (the OLD live head), head_repo,
head_ref, base_repo, base_ref, owner (AO/session owner), work_id, publisher_id and
publisher_login, plus reviewed_by/review_reference. Never derive this review from
PR bodies/labels or candidate assertions. The tool checks consistency, not the
authenticity of an operator's review or Git ancestry. Recheck immediately before
an ordinary same-branch push; this read-only check is not an atomic reservation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess


REPOSITORY = "MulattoTech/MJobs"
CATEGORIES = {"feature", "docs-maintenance", "security", "acceptance", "integration"}
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def positive(value):
    return type(value) is int and value > 0


def repository(value):
    """Canonical case-insensitive GitHub owner/name, or unknown."""
    return value.casefold() if isinstance(value, str) and REPO.fullmatch(value) else None


def validate_policy(policy):
    if not isinstance(policy, dict):
        return ["policy must be an object"]
    errors = []
    if not positive(policy.get("max_active_prs")):
        errors.append("max_active_prs must be a positive integer")
    # Legacy schema v1 policies without a behavior retain their hard-cap meaning.
    if policy.get("capacity_behavior", "block") not in ("warn", "block"):
        errors.append("capacity_behavior must be warn or block")
    for key in ("never_create_empty_prs", "open_draft_only_after_first_real_commit"):
        if key in policy and policy[key] is not True:
            errors.append(f"{key} cannot be disabled")
    return errors


def reviewed(pr, metadata):
    entry = metadata.get(str(pr.get("number"))) if isinstance(metadata, dict) else None
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    if (isinstance(entry, dict) and nonempty(entry.get("reviewed_by"))
            and nonempty(entry.get("review_reference")) and nonempty(head.get("sha"))
            and entry.get("head_sha") == head["sha"]):
        return entry
    return {}


def classify(pr, metadata):
    actor = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    # Pinned GitHub-owned Dependabot principal, observed via authenticated REST.
    # Title, branch, human publisher, labels and app-looking names confer no trust.
    dependency = (type(actor.get("id")) is int and actor["id"] == 49699333
                  and actor.get("type") == "Bot" and actor.get("login") == "dependabot[bot]")
    entry = reviewed(pr, metadata)
    return {"dependency_automation": dependency,
            "authorship": entry.get("authorship") if entry.get("authorship") in ("human", "AI") else "unknown",
            "category": entry.get("category") if isinstance(entry.get("category"), str) and entry["category"] in CATEGORIES else "unknown"}


def validate_update(open_prs, metadata, candidate, update_pr):
    """Bind one existing development PR to an independently reviewed owner.

    Only same-repository human-published PRs are supported. Fork/bot updates need
    a separately reviewed workflow; an update never exempts another PR.
    """
    if not positive(update_pr):
        return ["update requires a positive integer PR number"]
    if not isinstance(candidate, dict):
        return ["update requires candidate evidence"]
    matches = [pr for pr in open_prs if pr.get("number") == update_pr]
    if len(matches) != 1:
        return ["update requires exactly one existing open PR"]
    pr = matches[0]
    entry = reviewed(pr, metadata)
    if not entry:
        return ["update requires separate operator-reviewed metadata at the live old head"]
    errors = []
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    actor = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    if pr.get("merged_at") is not None or pr.get("merged") is True:
        errors.append("update target must be unmerged")
    old_sha = candidate.get("previous_head_sha")
    if not SHA.fullmatch(str(old_sha or "")) or old_sha != head.get("sha"):
        errors.append("update previous_head_sha must match the live old head")
    if candidate.get("head_sha") == head.get("sha"):
        errors.append("update requires a new candidate head")
    for key in ("owner", "work_id"):
        if not nonempty(entry.get(key)) or entry[key] != candidate.get(key):
            errors.append(f"update requires reviewed matching {key}")
    for name, section in (("head", head), ("base", base)):
        source = section.get("repo") if isinstance(section.get("repo"), dict) else {}
        repo_key, ref_key = f"{name}_repo", f"{name}_ref"
        if any(repository(value) != repository(REPOSITORY) for value in
               (source.get("full_name"), entry.get(repo_key), candidate.get(repo_key))):
            errors.append(f"update requires matching same-repository {repo_key}")
        ref = section.get("ref")
        if not nonempty(ref) or entry.get(ref_key) != ref or candidate.get(ref_key) != ref:
            errors.append(f"update requires reviewed matching {ref_key}")
    if (actor.get("type") != "User" or not positive(actor.get("id"))
            or not positive(entry.get("publisher_id"))
            or entry["publisher_id"] != actor.get("id")
            or not nonempty(actor.get("login"))
            or entry.get("publisher_login") != actor.get("login")):
        errors.append("update requires reviewed matching human publisher identity")
    return errors


def assess(prs, policy, metadata=None, candidate=None, *, update_pr=None):
    errors = validate_policy(policy)
    if not isinstance(policy, dict):
        policy = {}
    open_prs = []
    seen = set()
    if not isinstance(prs, list):
        errors.append("snapshot must be a complete REST pull request list")
        prs = []
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") not in ("open", "closed"):
            errors.append("malformed pull request")
            continue
        number = pr.get("number")
        if not positive(number) or number in seen:
            errors.append("missing or duplicate PR number")
        seen.add(number) if isinstance(number, int) else None
        if pr["state"] != "open":
            continue
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        if (type(pr.get("draft")) is not bool or not SHA.fullmatch(str(head.get("sha", "")))
                or not nonempty(head.get("ref"))):
            errors.append("incomplete open PR identity/draft data")
        open_prs.append(pr)
    classified = [(pr, classify(pr, metadata)) for pr in open_prs]
    development = [(pr, info) for pr, info in classified if not info["dependency_automation"]]
    validated_update = None
    if update_pr is not None:
        update_errors = validate_update(open_prs, metadata, candidate, update_pr)
        errors.extend(update_errors)
        if not update_errors:
            validated_update = update_pr
    projected = len(development) + (1 if candidate is not None and validated_update is None else 0)
    limit = policy.get("max_active_prs", 32) if isinstance(policy, dict) else 32
    valid_limit = positive(limit)
    capacity = "WARNING" if valid_limit and projected >= limit else "OK"
    if candidate is not None:
        if not isinstance(candidate, dict):
            errors.append("candidate must be an object")
            candidate = {}
        for key in ("head_ref", "owner", "work_id", "nonduplicate_review"):
            if not nonempty(candidate.get(key)):
                errors.append(f"candidate requires {key}")
        if not SHA.fullmatch(str(candidate.get("head_sha", ""))):
            errors.append("candidate requires full head_sha")
        candidate_repo = repository(candidate.get("head_repo"))
        if candidate_repo is None:
            errors.append("candidate requires qualified head_repo owner/name")
        for key in ("commit_count", "changed_files"):
            if not positive(candidate.get(key)):
                errors.append(f"candidate requires positive {key}")
        validation = candidate.get("local_validation")
        if not isinstance(validation, list) or not validation or not all(nonempty(v) for v in validation):
            errors.append("candidate requires executed local_validation evidence")
        if candidate.get("meaningful_change") is not True:
            errors.append("candidate requires reviewed meaningful_change")
        for pr in open_prs:
            if pr.get("number") == validated_update:
                continue
            head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
            source = head.get("repo") if isinstance(head.get("repo"), dict) else {}
            source_repo = repository(source.get("full_name"))
            matching_ref = head.get("ref") == candidate.get("head_ref")
            if matching_ref and source_repo is None:
                errors.append(f"unknown source repository for matching branch: {pr.get('number')}")
            if (head.get("sha") == candidate.get("head_sha")
                    or (matching_ref and candidate_repo is not None and source_repo == candidate_repo)
                    or (nonempty(candidate.get("work_id"))
                        and reviewed(pr, metadata).get("work_id") == candidate["work_id"])):
                errors.append(f"duplicate open PR scope/identity: {pr.get('number')}")
        if valid_limit and projected > limit and policy.get("capacity_behavior", "block") == "block":
            errors.append("projected development PR capacity exceeded")
    conflicts = {"conflicting": [], "clear": [], "unknown": []}
    for pr, _ in development:
        value = pr.get("mergeable")
        conflicts["clear" if value is True else "conflicting" if value is False else "unknown"].append(pr.get("number"))
    return {"development_open": len(development), "dependency_open": len(open_prs) - len(development),
            "drafts": sum(pr.get("draft") is True for pr, _ in development),
            "authorship": dict(Counter(info["authorship"] for _, info in development)),
            "categories": dict(Counter(info["category"] for _, info in development)),
            "backlog": [pr.get("number") for pr, _ in development], "conflicts": conflicts,
            "ci": "NOT_QUERIED", "projected_development_open": projected,
            "capacity": capacity, "errors": errors, "allowed": not errors,
            "publication_checked": candidate is not None,
            "publication_mode": "update" if update_pr is not None else "new" if candidate is not None else "inventory",
            "validated_update_pr": validated_update}


def github_pulls():
    """All pages, read-only fixed repository endpoint; fail on transport/auth errors."""
    result = subprocess.run(["gh", "api", "--hostname", "github.com", "--paginate", "--slurp",
                             f"repos/{REPOSITORY}/pulls?state=open&per_page=100"],
                            capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("invalid paginated GitHub response")
    return [pr for page in pages for pr in page]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=Path("planning/PR_QUEUE.json"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--live", action="store_true")
    source.add_argument("--snapshot", type=Path)
    parser.add_argument("--metadata", type=Path, help="operator-reviewed head-bound metadata")
    parser.add_argument("--candidate", type=Path, help="reviewed publication evidence (never publishes)")
    parser.add_argument("--update-pr", type=int, help="existing owned PR number; requires live inventory and separate reviewed metadata")
    args = parser.parse_args()
    if args.update_pr is not None and not (args.live and args.candidate and args.metadata):
        parser.error("--update-pr requires --live, --candidate and --metadata")
    try:
        read = lambda path: json.loads(path.read_text(encoding="utf-8"))
        candidate = read(args.candidate) if args.candidate else None
        if args.candidate and not isinstance(candidate, dict):
            raise ValueError("candidate must be an object")
        result = assess(github_pulls() if args.live else read(args.snapshot), read(args.queue)["policy"],
                        read(args.metadata) if args.metadata else {}, candidate, update_pr=args.update_pr)
        result["source"] = "authenticated GitHub REST" if args.live else "offline operator snapshot; freshness unverified"
        print(json.dumps(result, indent=2))
        return int(not result["allowed"])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(json.dumps({"allowed": False, "errors": ["unreadable policy/snapshot or GitHub query failed"]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
