"""Read-only projection of the MJ-REQ-292 campaign checkpoint."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

REQUIREMENT_ID = "MJ-REQ-292"
ISSUE_NUMBER = 293
ISSUE_URL = "https://github.com/MulattoTech/MJobs/issues/293"
ACCEPTANCE_CLAIM_SCOPE = "recorded ledger; independent runtime and merge gates remain separate"
MAX_LEDGER_BYTES = 512_000
MAX_HANDOFF_BYTES = 64_000
MAX_STRING = 500
STATUSES = {"PASS", "PARTIAL", "NOT_RUN", "FAIL", "BLOCKED"}
SCORE_KEYS = ("verified_pass", "PARTIAL", "NOT_RUN", "FAIL", "BLOCKED")
REVIEW_STATUSES = {"NOT_RUN", "PASS", "PARTIAL", "FAIL"}
MERGE_STATUSES = {"NOT_MERGED", "MERGED"}
PHASES = (("A", 1, 8), ("B", 9, 22), ("C", 23, 31),
          ("D", 32, 35), ("E", 36, 53), ("F", 54, 60))
STEP_HEADING = re.compile(r"^\s*(\d{1,2})\.\s+(.+?)\s*$", re.MULTILINE)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
PRIVATE_TEXT = re.compile(
    r"(?:[a-z]:\\|\\\\[^\\\s]+\\|/Users/|/home/|"
    r"-----BEGIN|\b(?:token|password|secret)\s*=)", re.IGNORECASE,
)
COMMAND_TEXT = re.compile(
    r"\b(?:python(?:\.exe)?\s+(?:-m\b|-c\b|[\w./\\-]+\.py\b)|"
    r"git\s+(?:-C\b|status\b|rev-parse\b|commit\b|push\b|checkout\b|worktree\b|diff\b|add\b)|"
    r"powershell(?:\.exe)?\s+(?:-|[A-Za-z])|npm\s+(?:--|install\b|run\b|test\b))",
    re.IGNORECASE,
)


class CampaignDataError(ValueError):
    """The checkpoint is absent, malformed, or internally inconsistent."""


def _safe_string(value: Any, *, limit: int = MAX_STRING) -> str:
    if not isinstance(value, str):
        raise CampaignDataError("invalid text")
    value = CONTROL.sub(" ", value).strip()
    if (not value or len(value) > limit or "\n" in value or "\r" in value
            or PRIVATE_TEXT.search(value)):
        raise CampaignDataError("invalid text")
    return value


def _safe_optional_prose(value: Any, *, limit: int = 240) -> str | None:
    """Validate bounded source prose before truncating its dashboard display."""
    if not isinstance(value, str) or len(value) > 4000:
        return None
    value = CONTROL.sub(" ", value).strip()
    if not value or PRIVATE_TEXT.search(value) or COMMAND_TEXT.search(value):
        return None
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _json_file(path: Path, limit: int) -> tuple[dict[str, Any], bytes]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if not raw or len(raw) > limit:
            raise CampaignDataError("invalid file size")
        value = json.loads(raw)
    except CampaignDataError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignDataError("unavailable checkpoint") from exc
    if not isinstance(value, dict):
        raise CampaignDataError("invalid checkpoint")
    return value, raw


def _canonical_titles(repo: Path) -> list[str]:
    seeds = repo / "planning" / "ISSUE_SEED.jsonl"
    if not seeds.is_file() or seeds.stat().st_size > 8_000_000:
        raise CampaignDataError("canonical steps unavailable")
    try:
        for line in seeds.read_text(encoding="utf-8").splitlines():
            if len(line) > 1_000_000:
                continue
            row = json.loads(line)
            if isinstance(row, dict) and row.get("id") == REQUIREMENT_ID:
                body = row.get("body")
                if not isinstance(body, str) or len(body) > 80_000:
                    break
                numbered = {int(number): title.strip() for number, title in STEP_HEADING.findall(body)}
                if set(numbered) == set(range(1, 61)):
                    return [numbered[i] for i in range(1, 61)]
                break
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CampaignDataError("canonical steps unavailable") from exc
    raise CampaignDataError("canonical steps unavailable")


def _requirement_catalog(repo: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Attach readable canonical seed titles to a fresh read-only matrix view."""
    from .progress_history import current_capture

    capture = current_capture(repo)
    title_by_id: dict[str, str] = {}
    seeds_path = repo / "planning" / "ISSUE_SEED.jsonl"
    if seeds_path.stat().st_size > 8_000_000:
        raise CampaignDataError("canonical catalog unavailable")
    for line in seeds_path.read_text(encoding="utf-8").splitlines():
        if len(line) > 1_000_000:
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        title = row.get("title")
        if isinstance(title, str):
            title = re.sub(r"^\[[^]]+\]\s*", "", title).strip()
            title = CONTROL.sub(" ", title).strip()
            if title and len(title) <= 180:
                title_by_id[row["id"]] = title
    items = []
    for rid, row in capture["requirements"].items():
        items.append({
            "id": rid,
            "title": title_by_id.get(rid, rid),
            "status": row["status"],
            "priority": row["priority"],
            "issue": row.get("issue"),
        })
    items.sort(key=lambda item: item["id"])
    overview = {
        "original": capture["original"],
        "reference": capture["reference"],
        "priorities": capture["priorities"],
    }
    return overview, items


def _git_revision(root: Path) -> str | None:
    """Read the checkout revision with a bounded git command and no path output."""
    try:
        from .delivery_ledger import _git
        revision = _git(root, "rev-parse", "--verify", "HEAD", timeout=2).strip()
        return revision[:40] if re.fullmatch(r"[0-9a-f]{40,64}", revision) else None
    except (OSError, RuntimeError, ValueError):
        return None


def _iso(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = _safe_string(value, limit=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise CampaignDataError("invalid timestamp") from exc


def _scoreboard(value: Any, steps: list[dict[str, Any]]) -> dict[str, int]:
    if not isinstance(value, dict):
        raise CampaignDataError("invalid scoreboard")
    counts = {key: 0 for key in SCORE_KEYS}
    for step in steps:
        key = "verified_pass" if step["status"] == "PASS" else step["status"]
        counts[key] += 1
    expected = {"total": len(steps), **counts}
    if set(value) != set(expected):
        raise CampaignDataError("scoreboard mismatch")
    for key, count in expected.items():
        if isinstance(value.get(key), bool) or value.get(key) != count:
            raise CampaignDataError("scoreboard mismatch")
    return expected


def _load_handoff(path: Path) -> dict[str, Any] | None:
    try:
        handoff, _ = _json_file(path, MAX_HANDOFF_BYTES)
    except CampaignDataError:
        return None
    identities = [handoff[key] for key in ("requirement_id", "task_id", "task") if key in handoff]
    if any(identity != REQUIREMENT_ID for identity in identities):
        return None
    return handoff


def _configured_source(app_root: Path, configured_root: str | None) -> tuple[Path, bool, str | None]:
    env_root = configured_root if configured_root is not None else os.environ.get("MJOBS_CAMPAIGN_ROOT")
    if env_root:
        if len(env_root) > 1024:
            return app_root, True, "Campaign source configuration is invalid."
        return Path(env_root).expanduser().resolve(), True, None
    local_app_data = os.environ.get("LOCALAPPDATA")
    config_path = (Path(local_app_data) / "MJobs" / "MissionControl" / "campaign-source.json"
                   if local_app_data else None)
    if config_path is None or not config_path.is_file():
        return app_root, False, None
    try:
        config, _ = _json_file(config_path, 4096)
        root_value = config.get("root")
        if config.get("schema_version") != 1 or not isinstance(root_value, str) or not root_value.strip() or len(root_value) > 1024:
            raise CampaignDataError("invalid source config")
        return Path(root_value).expanduser().resolve(), True, None
    except (CampaignDataError, OSError, ValueError):
        return app_root, True, "Campaign source configuration is invalid."


def campaign_progress(repo: Path, *, configured_root: str | None = None,
                      observed_at: datetime | None = None) -> dict[str, Any]:
    """Return one campaign checkpoint; never mutates the ledger or its history."""
    app_root = Path(repo).resolve()
    source_root, configured, config_error = _configured_source(app_root, configured_root)
    checkpoint = source_root / "planning" / "acceptance" / f"{REQUIREMENT_ID}.json"
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: dict[str, Any] = {
        "schema_version": 1,
        "available": False,
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "campaigns": [],
    }
    try:
        result["overview"], result["requirements"] = _requirement_catalog(app_root)
    except (CampaignDataError, OSError, ValueError, TypeError, KeyError):
        # A missing campaign ledger remains distinguishable from missing canonical catalog data.
        pass
    if config_error:
        result["error"] = config_error
        return result
    try:
        ledger, raw = _json_file(checkpoint, MAX_LEDGER_BYTES)
        if ledger.get("schema_version") != 1 or ledger.get("requirement_id") != REQUIREMENT_ID:
            raise CampaignDataError("wrong requirement")
        titles = _canonical_titles(app_root)
        raw_steps = ledger.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) != 60:
            raise CampaignDataError("invalid steps")
        seen: set[int] = set()
        phase_map: dict[str, dict[str, Any]] = {}
        steps: list[dict[str, Any]] = []
        for item in raw_steps:
            if not isinstance(item, dict):
                raise CampaignDataError("invalid step")
            number = item.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 60 or number in seen:
                raise CampaignDataError("invalid step number")
            status = item.get("status")
            if status not in STATUSES:
                raise CampaignDataError("invalid step status")
            if item.get("original_requirement") != titles[number - 1]:
                raise CampaignDataError("noncanonical step title")
            seen.add(number)
            phase_id = _safe_string(item.get("phase"), limit=40)
            expected_phase = next((phase_id_expected for phase_id_expected, start, end in PHASES
                                   if start <= number <= end), None)
            if phase_id != expected_phase:
                raise CampaignDataError("invalid phase membership")
            phase = phase_map.setdefault(phase_id, {
                "id": phase_id, "label": f"Phase {phase_id}", "steps": [],
            })
            step = {"number": number, "title": titles[number - 1], "status": status}
            remaining_gap = _safe_optional_prose(item.get("missing_behavior"))
            if remaining_gap:
                step["remaining_gap"] = remaining_gap
            verification = item.get("verification")
            if verification is not None:
                if not isinstance(verification, dict):
                    raise CampaignDataError("invalid verification")
                if verification.get("observed_result") is not None:
                    observed = _safe_optional_prose(verification["observed_result"])
                    if observed:
                        step["observed_result"] = observed
                tested = verification.get("tested_commit")
                if tested is not None:
                    tested = _safe_string(tested, limit=64)
                    if not re.fullmatch(r"[0-9a-f]{7,64}", tested):
                        raise CampaignDataError("invalid tested revision")
                    step["tested_commit"] = tested
                if verification.get("executed_at_utc") is not None:
                    step["executed_at_utc"] = _iso(verification["executed_at_utc"])
            for field, source in (("review_status", "review"), ("merge_status", "merge")):
                state = item.get(source)
                if state is not None:
                    if not isinstance(state, dict):
                        raise CampaignDataError("invalid review or merge state")
                    status_value = state.get("status")
                    allowed = REVIEW_STATUSES if source == "review" else MERGE_STATUSES
                    if status_value in allowed:
                        step[field] = status_value
            phase["steps"].append(step)
            steps.append(step)
        if seen != set(range(1, 61)):
            raise CampaignDataError("incomplete step set")
        steps.sort(key=lambda row: row["number"])
        phases = list(phase_map.values())
        for phase in phases:
            phase["steps"].sort(key=lambda row: row["number"])
        phases.sort(key=lambda phase: phase["id"])
        score = _scoreboard(ledger.get("scoreboard"), steps)
        updated = _iso(ledger.get("updated_at_utc"))
        ledger_revision = ledger.get("source_revision")
        handoff_path = source_root / "planning" / "agent_handoffs" / f"{REQUIREMENT_ID}.json"
        handoff = _load_handoff(handoff_path) if handoff_path.is_file() else None
        handoff_revision = None
        if handoff and handoff.get("head_sha") is not None:
            handoff_revision = _safe_string(handoff["head_sha"], limit=64)
            if not re.fullmatch(r"[0-9a-f]{7,64}", handoff_revision):
                handoff_revision = None
        handoff_ledger_hash = None
        ledger_hash_match = None
        if handoff and handoff.get("acceptance_ledger_sha256") is not None:
            candidate_hash = handoff.get("acceptance_ledger_sha256")
            if isinstance(candidate_hash, str) and re.fullmatch(r"[0-9a-f]{64}", candidate_hash):
                handoff_ledger_hash = candidate_hash
                ledger_hash_match = candidate_hash == hashlib.sha256(raw).hexdigest()
        source_revision = _git_revision(source_root)
        blockers_raw = ledger.get("blockers", [])
        if not isinstance(blockers_raw, list) or len(blockers_raw) > 40:
            raise CampaignDataError("invalid blockers")
        blockers = []
        for item in blockers_raw:
            text = _safe_optional_prose(item)
            if text:
                blockers.append(text)
        handoff_blockers = handoff.get("blockers", []) if handoff else []
        if isinstance(handoff_blockers, list):
            for blocker in handoff_blockers[:40]:
                text = _safe_optional_prose(blocker)
                if text:
                    blockers.append("Recorded handoff: " + text)
        if handoff and handoff.get("acceptance_ledger_sha256") is not None and handoff_ledger_hash is None:
            raise CampaignDataError("invalid handoff ledger digest")
        if ledger_hash_match is False:
            raise CampaignDataError("handoff ledger digest mismatch")
        if (isinstance(ledger_revision, str) and re.fullmatch(r"[0-9a-f]{7,64}", ledger_revision)
                and source_revision and not source_revision.startswith(ledger_revision)):
            blockers.append("Ledger revision differs from the current checkout revision.")
        if handoff_revision and source_revision and not source_revision.startswith(handoff_revision):
            blockers.append("Handoff revision differs from the current checkout revision.")
        next_action = _safe_optional_prose(handoff.get("next_action")) if handoff else None
        item = {
            "requirement_id": REQUIREMENT_ID,
            "title": _safe_string(ledger.get("title", "MJ-REQ-292: Complete connected user journey"), limit=180),
            "issue_url": ISSUE_URL,
            "updated_at_utc": updated,
            "source_revision": source_revision,
            "source_kind": "working_checkpoint" if configured else "checkout",
            "ledger_sha256": hashlib.sha256(raw).hexdigest(),
            "acceptance_claim_scope": ACCEPTANCE_CLAIM_SCOPE,
            "scoreboard": score,
            "phases": phases,
            "blockers": blockers,
            "next_action": next_action or "No next action recorded.",
        }
        if handoff_revision:
            item["handoff_revision"] = handoff_revision
        if handoff_ledger_hash:
            item["handoff_ledger_sha256"] = handoff_ledger_hash
            item["ledger_hash_match"] = ledger_hash_match
        if handoff:
            routing = handoff.get("routing_state")
            if routing in {"EXECUTE", "RESUME", "DELEGATE", "VERIFY", "INTEGRATE", "REPLAN",
                           "PARTIALLY_BLOCKED", "BLOCKED", "COMPLETE", "PAUSED"}:
                item["routing_state"] = routing
                item["routing_state_scope"] = "recorded checkpoint; not live process state"
            session_state = handoff.get("session_state")
            if session_state in {"RUNNING", "PAUSED", "STOPPED", "NOT_STARTED", "UNKNOWN"}:
                item["session_state"] = session_state
        result.update(available=True, campaigns=[item])
    except (CampaignDataError, OSError, ValueError, TypeError):
        result["error"] = "Campaign checkpoint is unavailable or inconsistent."
    return result
