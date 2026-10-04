"""Fresh-context Codex loop with durable anti-thrash and independent verification."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from afk_redact import redact, sanitize
from afk_state import atomic_json, event, initialize, read_json, set_goal, state_root, status

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "references" / "iteration-result.schema.json"
REQUIRED = {"status", "progressMade", "workItem", "summary", "evidence", "failureFingerprint", "changedCondition", "nextAction", "externalBlocker", "lessonCandidates"}
TRANSIENT = ("rate limit", "rate_limit", "429", "temporarily unavailable")


@contextlib.contextmanager
def runner_lock(root):
    """OS-held single-owner lock; process termination releases it."""
    path = Path(root) / "run.owner.lock"
    with path.open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another AFK runner owns {root}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def contract(goal):
    def obligation(items):
        return [{k: v for k, v in item.items() if k not in {"passed", "evidence", "verifiedUtc", "headSha", "sourceFingerprint"}} for item in items]
    return {"statement": goal.get("statement"), "constraints": goal.get("constraints", []), "verificationCommands": goal.get("verificationCommands", []), "acceptanceCriteria": obligation(goal.get("acceptanceCriteria", [])), "browserJourneys": obligation(goal.get("browserJourneys", [])), "ciRequirements": obligation(goal.get("ciRequirements", []))}


def contract_hash(goal):
    return hashlib.sha256(json.dumps(contract(goal), sort_keys=True).encode("utf-8")).hexdigest()


def source_fingerprint(project):
    git = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True)
    if git.returncode:
        return None, None
    head = git.stdout.strip()
    status_result = subprocess.run(["git", "-C", str(project), "status", "--porcelain=v1", "--untracked-files=all", "-z"], capture_output=True)
    diff_result = subprocess.run(["git", "-C", str(project), "diff", "--binary", "HEAD"], capture_output=True)
    if status_result.returncode or diff_result.returncode:
        return None, None
    untracked = bytearray()
    for entry in status_result.stdout.split(b"\0"):
        if not entry.startswith(b"?? "):
            continue
        name = os.fsdecode(entry[3:])
        path = Path(project) / name
        if not path.resolve().is_relative_to(Path(project).resolve()):
            return None, None
        untracked.extend(entry)
        if path.is_symlink():
            untracked.extend(os.readlink(path).encode("utf-8", errors="replace"))
        elif path.is_file():
            untracked.extend(hashlib.sha256(path.read_bytes()).digest())
    payload = head.encode() + status_result.stdout + diff_result.stdout + bytes(untracked)
    return head, hashlib.sha256(payload).hexdigest()


def validate_result(value):
    if not isinstance(value, dict) or set(value) != REQUIRED:
        raise ValueError("Malformed iteration result fields")
    if value["status"] not in {"continue", "complete", "blocked", "paused", "handoff"} or not isinstance(value["progressMade"], bool):
        raise ValueError("Malformed iteration status")
    if value["status"] == "handoff" and value["externalBlocker"]:
        raise ValueError("Handoff cannot conceal an external blocker")
    if not isinstance(value["evidence"], list) or not isinstance(value["lessonCandidates"], list):
        raise ValueError("Malformed iteration lists")
    return value


def make_prompt(project, root, iteration, changed_condition="", recovery=False, escalate=False, browser="auto", ci="auto", handoff=False):
    return (
        "Use $afk-dev.\n"
        f"Project: {project}\nAFK state: {root}\nGoal: read goal.json; do not replace it from memory.\n"
        f"Iteration: {iteration}\nChanged condition: {changed_condition or 'none recorded'}\n"
        f"Interrupted prior iteration: {recovery}. Handoff from prior iteration: {handoff}. Strategy escalation required: {escalate}.\n"
        f"Browser mode: {browser}; CI mode: {ci}. 'off' forbids those actions for this run.\n"
        "Read current project instructions and AFK state. Reconcile git and active work. "
        "If the goal has no acceptance criteria yet, normalize the user goal before changing code. "
        "Advance the single highest-value unmet work item. Verify lesson selectors and do not repeat an unchanged deterministic failure. "
        "A brief terminal or conversation transfer is a handoff, not a stop: finish bounded in-flight work, keep safe authorized work moving, and return status=handoff. "
        "Before shared or lab mutation after a handoff, verify the current token, single owner, and project authority. "
        "Use status=paused only for an explicit stop-all-work request; use blocked for a real external prerequisite. "
        "Persist meaningful events and lessons. If code changes, verify focused behavior. "
        "If acceptance appears complete, run current verification before returning complete. "
        "Return only the requested iteration-result schema."
    )


def codex_invoke(project, prompt, *, model, effort, artifact):
    executable = shutil.which("codex")
    if executable is None:
        raise FileNotFoundError("codex executable unavailable")
    cmd = [executable, "exec", "--approve-for-me", "-C", str(project), "--json", "--output-schema", str(SCHEMA_PATH)]
    if model:
        cmd.extend(["-m", model])
    if effort:
        cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
    cmd.append(prompt)
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    out, err = process.communicate()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if process.returncode:
        raise RuntimeError(f"codex exec exit {process.returncode}: {redact(err)[-500:]}")
    messages = []
    summaries = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        item = row.get("item", {})
        summaries.append({"type": row.get("type"), "itemType": item.get("type") if isinstance(item, dict) else None})
        if row.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item.get("text", ""))
    final_value = json.loads(messages[-1]) if messages else None
    artifact.write_text(json.dumps(sanitize({"eventSummary": summaries, "final": final_value}), ensure_ascii=False), encoding="utf-8")
    if not messages:
        raise ValueError("codex exec returned no final agent message")
    return validate_result(final_value)


def completion_oracle(project, goal, baseline=None, browser="auto", ci="auto"):
    if baseline is not None and contract_hash(goal) != baseline:
        return False, ["Goal contract changed during the run"]
    if browser == "off" and goal.get("browserJourneys"):
        return False, ["Browser verification disabled for this run"]
    if ci == "off" and goal.get("ciRequirements"):
        return False, ["CI verification disabled for this run"]
    head, source = source_fingerprint(project)
    for key in ("acceptanceCriteria", "browserJourneys", "ciRequirements"):
        obligations = goal.get(key, [])
        for item in obligations:
            if not isinstance(item, dict) or item.get("passed") is not True or not item.get("evidence"):
                return False, [f"Unverified {key} obligation"]
            if not item.get("verificationCommand"):
                return False, [f"No independent verification command for {key}"]
            if key == "ciRequirements" and (not head or item.get("headSha") != head):
                return False, ["CI evidence is not for current HEAD"]
            if key == "browserJourneys" and (not source or item.get("sourceFingerprint") != source):
                return False, ["Browser evidence is not for current source"]
    commands = goal.get("verificationCommands", [])
    for key in ("acceptanceCriteria", "browserJourneys", "ciRequirements"):
        commands = commands + [item["verificationCommand"] for item in goal.get(key, [])]
    if not commands:
        return False, ["No independent verification command is defined"]
    evidence = []
    for command in commands:
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            return False, ["Verification command must be an argv array"]
        try:
            result = subprocess.run(command, cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, [f"Verification command unavailable or timed out: {redact(str(exc))}"]
        evidence.append(f"{command[0]} exit={result.returncode}: {redact((result.stdout + result.stderr)[-300:])}")
        if result.returncode:
            return False, evidence
    return True, evidence


def run_loop(project, **kwargs):
    root = initialize(project)
    with runner_lock(root):
        return _run_loop_unlocked(project, **kwargs)


def _run_loop_unlocked(project, *, goal_text=None, verification=None, model=None, effort=None, max_iterations=0, max_stagnant=3, browser="auto", ci="auto", invoke=codex_invoke, sleep=time.sleep):
    project = Path(project).resolve(strict=True)
    root = initialize(project)
    if goal_text:
        set_goal(project, goal_text, verification=verification or [])
    goal = read_json(root / "goal.json")
    if not goal:
        raise ValueError("No goal.json; provide --goal or --goal-file")
    run_path = root / "run.json"
    run = read_json(run_path, {"schemaVersion": 1, "runId": str(uuid.uuid4()), "iteration": 0, "stagnant": 0, "inFlight": False})
    if run.get("goalId") and run["goalId"] != goal["goalId"]:
        archive = root / "snapshots" / f"run-{run['runId']}.json"
        atomic_json(archive, run)
        run = {"schemaVersion": 1, "runId": str(uuid.uuid4()), "iteration": 0, "stagnant": 0, "inFlight": False}
    run["goalId"] = goal["goalId"]
    baseline = run.get("goalContractHash") or contract_hash(goal)
    if run.get("contractFinalized", True) and contract_hash(goal) != baseline:
        raise ValueError("Goal contract changed since the prior iteration")
    run["goalContractHash"] = baseline
    if "contractFinalized" not in run:
        run["contractFinalized"] = any(goal.get(key) for key in ("acceptanceCriteria", "browserJourneys", "ciRequirements"))
    original_core = run.get("goalCore") or {key: goal.get(key) for key in ("statement", "constraints", "verificationCommands")}
    run["goalCore"] = original_core
    if any(goal.get(key) != value for key, value in original_core.items()):
        raise ValueError("Goal core changed since the prior iteration")
    recovery = bool(run.get("inFlight"))
    if recovery:
        event(project, "recovery", "interrupted iteration requires git/process reconciliation", "UNKNOWN")
    while max_iterations == 0 or run["iteration"] < max_iterations:
        run["iteration"] += 1
        iteration = run["iteration"]
        prompt = make_prompt(project, root, iteration, run.get("changedCondition") or "", recovery, run.get("escalate", False), browser, ci, bool(run.get("handoffPending")))
        run["inFlight"] = True
        atomic_json(run_path, run)
        artifact = root / "artifacts" / run["runId"] / f"iteration-{iteration:04d}.jsonl"
        try:
            result = validate_result(invoke(project, prompt, model=model, effort=effort, artifact=artifact))
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            run["inFlight"] = False
            run["nextAction"] = f"Investigate invocation failure: {redact(str(exc))}"
            atomic_json(run_path, run)
            event(project, "recovery", "codex iteration invocation", "BLOCKED", evidence=[redact(str(exc))], iteration=iteration)
            return 2
        run["inFlight"] = False
        run["handoffPending"] = False
        run["nextAction"] = result["nextAction"]
        run["changedCondition"] = result["changedCondition"]
        if not run["contractFinalized"]:
            normalized = read_json(root / "goal.json")
            if any(normalized.get(key) != value for key, value in original_core.items()):
                run["nextAction"] = "Goal statement, constraints, or verification commands changed during normalization"
                atomic_json(run_path, run)
                return 2
            if any(normalized.get(key) for key in ("acceptanceCriteria", "browserJourneys", "ciRequirements")):
                baseline = contract_hash(normalized)
                run["goalContractHash"] = baseline
                run["contractFinalized"] = True
        effective_progress = result["progressMade"] and result["status"] != "handoff"
        if result["status"] != "complete":
            run["stagnant"] = 0 if effective_progress else run.get("stagnant", 0) + 1
        event(project, "review", result["summary"], "PROGRESS" if effective_progress else "UNKNOWN", evidence=result["evidence"], fingerprint=result["failureFingerprint"], run_id=run["runId"], iteration=iteration)
        fp = result["failureFingerprint"]
        if fp:
            prior = run.get("lastFailure")
            current = {"fingerprint": fp, "changedCondition": result["changedCondition"]}
            if prior == current:
                run["repeatedFailureCount"] = run.get("repeatedFailureCount", 0) + 1
                run["escalate"] = True
                if run["repeatedFailureCount"] >= 2:
                    run["nextAction"] = "Blocked unchanged deterministic failure; change strategy or condition before resuming"
                    atomic_json(run_path, run)
                    return 2
            else:
                run["repeatedFailureCount"] = 0
            run["lastFailure"] = current
        if result["status"] == "complete":
            current_goal = read_json(root / "goal.json")
            if not run["contractFinalized"]:
                baseline = contract_hash(current_goal)
            passed, oracle_evidence = completion_oracle(project, current_goal, baseline, browser, ci)
            event(project, "verify", "independent completion oracle", "PASS" if passed else "FAIL", evidence=oracle_evidence, iteration=iteration)
            if passed:
                run["contractFinalized"] = True
                run["goalContractHash"] = baseline
                goal = read_json(root / "goal.json")
                goal["completionState"] = "COMPLETE"
                atomic_json(root / "goal.json", goal)
                atomic_json(run_path, run)
                return 0
            run["nextAction"] = "Repair current completion-oracle failure"
            run["stagnant"] = run.get("stagnant", 0) + 1
        if result["status"] == "paused":
            run["externalBlocker"] = result.get("externalBlocker") or "paused"
            atomic_json(run_path, run)
            return 3
        if result["status"] == "handoff":
            run.pop("externalBlocker", None)
            run["handoffPending"] = True
            event(project, "handoff", result["summary"], "CONTINUE", evidence=result["evidence"], iteration=iteration)
        if result["status"] == "blocked":
            blocker = result.get("externalBlocker") or "unspecified blocker"
            if any(token in blocker.lower() for token in TRANSIENT):
                delay = min(2 ** min(run.get("transientCount", 0), 5), 30)
                run["transientCount"] = run.get("transientCount", 0) + 1
                if run["transientCount"] > 3:
                    atomic_json(run_path, run)
                    return 2
                run["wakeCondition"] = f"transient backoff {delay}s then retry"
                atomic_json(run_path, run)
                sleep(delay)
                continue
            run["externalBlocker"] = blocker
            atomic_json(run_path, run)
            return 2
        if run["stagnant"] >= max_stagnant:
            if run.get("escalate"):
                run["nextAction"] = "Stagnant after strategy escalation; external review required"
                atomic_json(run_path, run)
                return 2
            run["escalate"] = True
            run["stagnant"] = 0
        atomic_json(run_path, run)
        recovery = False
    return 3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "status"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--goal")
    parser.add_argument("--goal-file")
    parser.add_argument("--verify", action="append", help="JSON argv array, e.g. '[\"python\",\"-m\",\"unittest\"]'")
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max", "ultra"])
    parser.add_argument("--max-iterations", type=int, default=0)
    parser.add_argument("--max-stagnant", type=int, default=3)
    parser.add_argument("--browser", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--ci", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(args.project), indent=2))
        return 0
    goal = args.goal or (Path(args.goal_file).read_text(encoding="utf-8") if args.goal_file else None)
    verification = [json.loads(item) for item in (args.verify or [])]
    if args.dry_run:
        print(make_prompt(Path(args.project).resolve(), state_root(args.project), 1))
        return 0
    try:
        return run_loop(args.project, goal_text=goal, verification=verification, model=args.model, effort=args.effort, max_iterations=args.max_iterations, max_stagnant=args.max_stagnant, browser=args.browser, ci=args.ci)
    except KeyboardInterrupt:
        return 3


if __name__ == "__main__":
    sys.exit(main())
