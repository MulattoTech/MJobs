"""Durable project state and scoped learning for AFK-DEV (standard library only)."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from afk_redact import sanitize

SCHEMA = 1
DEFAULT_PROMOTION = {"distinctProjects": 2, "successfulApplications": 3, "maxContradictions": 0}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def local_base():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "AFK-DEV"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "afk-dev"


def canonical_project(project):
    return Path(project).expanduser().resolve(strict=True)


def _git(project, *args):
    return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=False)


def project_id(project):
    return hashlib.sha256(str(canonical_project(project)).casefold().encode("utf-8")).hexdigest()


def state_root(project):
    project = canonical_project(project)
    top = _git(project, "rev-parse", "--show-toplevel")
    common = _git(project, "rev-parse", "--git-common-dir")
    if top.returncode == 0 and common.returncode == 0:
        git_dir = Path(common.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = project / git_dir
        return git_dir.resolve() / "afk-dev"
    return local_base() / "projects" / project_id(project)


def knowledge_root():
    return local_base() / "knowledge"


@contextlib.contextmanager
def lock(root, timeout=10):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".write-lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"AFK state lock held: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        path.rmdir()


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Malformed AFK state preserved at {path}: {exc}") from exc


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(sanitize(data), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def append_jsonl(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(sanitize(data), sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"Malformed AFK journal preserved at {path}:{number}: {exc}") from exc
    return rows


def initialize(project):
    root = state_root(project)
    with lock(root):
        path = root / "project.json"
        if not path.exists():
            atomic_json(path, {"schemaVersion": SCHEMA, "projectId": project_id(project), "root": str(canonical_project(project)), "createdUtc": utcnow()})
        if not (root / "guardrails.json").exists():
            atomic_json(root / "guardrails.json", {"schemaVersion": SCHEMA, "promotion": DEFAULT_PROMOTION, "quarantineAfterContradictions": 2})
    return root


def set_goal(project, statement, acceptance=None, verification=None, constraints=None, authorization=None):
    root = initialize(project)
    path = root / "goal.json"
    with lock(root):
        previous = read_json(path)
        if previous and previous.get("completionState") != "COMPLETE":
            if previous.get("statement") != statement:
                raise ValueError("Existing active goal differs; explicit goal transition required")
            return previous
        goal = {"schemaVersion": SCHEMA, "goalId": str(uuid.uuid4()), "statement": statement, "acceptanceCriteria": acceptance or [], "constraints": constraints or [], "verificationCommands": verification or [], "browserJourneys": [], "ciRequirements": [], "externalActionsAllowed": authorization or {}, "completionState": "IN_PROGRESS", "createdUtc": utcnow()}
        atomic_json(path, goal)
    return goal


def event(project, phase, action, outcome, evidence=None, fingerprint=None, run_id=None, iteration=0, actor=None):
    root = initialize(project)
    record = {"schemaVersion": SCHEMA, "eventId": str(uuid.uuid4()), "timestampUtc": utcnow(), "projectId": project_id(project), "runId": run_id, "iteration": iteration, "actor": actor or {"kind": "root", "name": "codex"}, "phase": phase, "action": action, "outcome": outcome, "failureFingerprint": fingerprint, "evidence": evidence or [], "sensitiveContentStored": False}
    with lock(root):
        append_jsonl(root / "events.jsonl", record)
    return record


def fingerprint(fields):
    stable = {k: fields.get(k) for k in ("family", "exitCode", "check", "jobId", "exception", "source", "category", "toolVersion") if fields.get(k) is not None}
    if not stable:
        raise ValueError("At least one stable failure identity field is required")
    payload = json.dumps(sanitize(stable), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def record_failure(project, fields, strategy, condition="", classification="ambiguous"):
    root = initialize(project)
    fp = fingerprint(fields)
    row = {"schemaVersion": SCHEMA, "attemptId": str(uuid.uuid4()), "timestampUtc": utcnow(), "failureFingerprint": fp, "failureIdentity": fields, "strategy": strategy, "changedCondition": condition, "classification": classification, "outcome": "FAIL"}
    with lock(root):
        append_jsonl(root / "attempts.jsonl", row)
    event(project, "recovery", strategy, "FAIL", fingerprint=fp)
    return row


def retry_decision(project, failure_fp, changed_condition="", transient=False):
    attempts = [a for a in read_jsonl(state_root(project) / "attempts.jsonl") if a.get("failureFingerprint") == failure_fp and a.get("outcome") == "FAIL"]
    if not attempts:
        return "allowed"
    if changed_condition and changed_condition != attempts[-1].get("changedCondition"):
        return "allowed"
    if transient and len(attempts) < 3:
        return "backoff"
    return "escalate" if len(attempts) < 2 else "blocked-unchanged"


def add_lesson(project, *, failure_fp, task_class, selectors, symptom, root_cause, cause_status, ineffective, successful, validation, confidence="medium"):
    if cause_status not in ("proven", "hypothesis"):
        raise ValueError("cause_status must be proven or hypothesis")
    if not validation or not successful:
        raise ValueError("Lesson requires successful approach and validation evidence")
    root = initialize(project)
    row = {"schemaVersion": SCHEMA, "lessonId": str(uuid.uuid4()), "scope": "project", "projectId": project_id(project), "trigger": {"failureFingerprint": failure_fp, "taskClass": task_class, "environmentSelectors": selectors}, "symptom": symptom, "rootCause": root_cause, "causeStatus": cause_status, "ineffectiveApproaches": ineffective, "successfulApproach": successful, "validation": validation, "confidence": confidence, "occurrences": 1, "successfulReuses": 0, "contradictions": 0, "quarantined": False, "firstSeenUtc": utcnow(), "lastSeenUtc": utcnow(), "invalidationCriteria": []}
    with lock(root):
        append_jsonl(root / "lessons.jsonl", row)
    return row


def current_lessons(path):
    latest = {}
    for row in read_jsonl(path):
        latest[row["lessonId"]] = row
    return list(latest.values())


def match_lessons(project, failure_fp, task_class, environment):
    paths = [state_root(project) / "lessons.jsonl", knowledge_root() / "lessons.jsonl"]
    matches = []
    for path in paths:
        for row in current_lessons(path):
            trigger = row.get("trigger", {})
            selectors = trigger.get("environmentSelectors", {})
            if row.get("quarantined") or trigger.get("taskClass") != task_class:
                continue
            if trigger.get("failureFingerprint") not in (None, failure_fp):
                continue
            if all(environment.get(k) == v for k, v in selectors.items()):
                matches.append(row)
    return matches


def reuse_result(project, lesson_id, succeeded, evidence):
    root = initialize(project)
    path = root / "lessons.jsonl"
    with lock(root):
        matches = [x for x in current_lessons(path) if x["lessonId"] == lesson_id]
        if not matches:
            raise KeyError(lesson_id)
        row = dict(matches[0])
        if succeeded:
            row["successfulReuses"] += 1
            row["confidence"] = "high" if row["successfulReuses"] >= 2 and row["contradictions"] == 0 else row["confidence"]
        else:
            row["contradictions"] += 1
            row["confidence"] = "low"
            guard = read_json(root / "guardrails.json", {})
            if row["contradictions"] >= guard.get("quarantineAfterContradictions", 2):
                row["quarantined"] = True
        row["lastSeenUtc"] = utcnow()
        row["validation"] = row["validation"] + [evidence]
        append_jsonl(path, row)
    return row


def promote_lesson(lesson, applications, rule=None):
    """Promote only with caller-supplied validated application evidence across projects."""
    rule = rule or DEFAULT_PROMOTION
    project_ids = {a["projectId"] for a in applications if a.get("validated") is True}
    if lesson.get("quarantined") or lesson.get("contradictions", 0) > rule["maxContradictions"]:
        raise ValueError("Contradicted lesson cannot be promoted")
    if len(project_ids) < rule["distinctProjects"] or len([a for a in applications if a.get("validated") is True]) < rule["successfulApplications"]:
        raise ValueError("Insufficient cross-project validation")
    if lesson.get("causeStatus") != "proven" or re.search(r"[A-Za-z]:\\|[/\\]Users[/\\]|\bRunId\b", json.dumps(lesson)):
        raise ValueError("Lesson is not portable")
    portable = dict(lesson)
    portable["scope"] = "portable"
    portable.pop("projectId", None)
    portable["promotionEvidence"] = applications
    root = knowledge_root()
    with lock(root):
        append_jsonl(root / "lessons.jsonl", portable)
    return portable


def status(project):
    root = state_root(project)
    goal = read_json(root / "goal.json", {})
    run = read_json(root / "run.json", {})
    attempts = read_jsonl(root / "attempts.jsonl")
    return {"goal": goal.get("statement"), "state": goal.get("completionState", "UNINITIALIZED"), "iteration": run.get("iteration", 0), "acceptance": f"{sum(bool(x.get('passed')) for x in goal.get('acceptanceCriteria', []) if isinstance(x, dict))}/{len(goal.get('acceptanceCriteria', []))}", "lastFailure": attempts[-1].get("failureFingerprint") if attempts else None, "nextAction": run.get("nextAction"), "stateRoot": str(root)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "status", "goal-set", "event", "failure", "success", "lesson-add", "lesson-match", "lesson-promote", "guardrail-add", "doctor", "export", "queue"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--statement")
    parser.add_argument("--verification", action="append", default=[])
    parser.add_argument("--json", dest="json_value")
    args = parser.parse_args(argv)
    if args.command == "init":
        result = {"stateRoot": str(initialize(args.project))}
    elif args.command == "status":
        result = status(args.project)
    elif args.command == "goal-set":
        if not args.statement:
            parser.error("goal-set requires --statement")
        result = set_goal(args.project, args.statement, verification=[json.loads(x) for x in args.verification])
    elif args.command == "doctor":
        root = state_root(args.project)
        result = {"stateRoot": str(root), "files": {p.name: "valid" for p in root.glob("*.json") if read_json(p) is not None}, "tempFiles": [p.name for p in root.glob(".*.tmp")]}
    elif args.command == "export":
        result = {"status": status(args.project), "events": read_jsonl(state_root(args.project) / "events.jsonl"), "lessons": current_lessons(state_root(args.project) / "lessons.jsonl")}
    else:
        if not args.json_value:
            parser.error(f"{args.command} requires --json")
        data = json.loads(args.json_value)
        if args.command in ("event", "success"):
            result = event(args.project, data.get("phase", "verify"), data["action"], data.get("outcome", "PASS" if args.command == "success" else "UNKNOWN"), data.get("evidence"), data.get("failureFingerprint"))
        elif args.command == "failure":
            result = record_failure(args.project, data["fields"], data["strategy"], data.get("changedCondition", ""), data.get("classification", "ambiguous"))
        elif args.command == "lesson-add":
            result = add_lesson(args.project, failure_fp=data["failureFingerprint"], task_class=data["taskClass"], selectors=data.get("environmentSelectors", {}), symptom=data["symptom"], root_cause=data["rootCause"], cause_status=data["causeStatus"], ineffective=data.get("ineffectiveApproaches", []), successful=data["successfulApproach"], validation=data["validation"])
        elif args.command == "lesson-match":
            result = match_lessons(args.project, data["failureFingerprint"], data["taskClass"], data.get("environment", {}))
        elif args.command == "lesson-promote":
            guard = read_json(state_root(args.project) / "guardrails.json", {"promotion": DEFAULT_PROMOTION})
            result = promote_lesson(data["lesson"], data["applications"], guard["promotion"])
        elif args.command == "guardrail-add":
            root = initialize(args.project)
            with lock(root):
                guard = read_json(root / "guardrails.json")
                guard.update(data)
                atomic_json(root / "guardrails.json", guard)
            result = guard
        elif args.command == "queue":
            root = initialize(args.project)
            with lock(root):
                atomic_json(root / "queue.json", {"schemaVersion": SCHEMA, "items": data})
            result = data
    print(json.dumps(sanitize(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
