"""Task-scoped, least-data projections over immutable career-profile versions."""
from __future__ import annotations

import json
import re
from uuid import UUID

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now


class ProfileAccessDenied(ValueError):
    """A task requested profile data outside its approved scope."""


# No general-purpose profile reader is registered. Add a task only with a
# minimal, reviewable set of fields; nested work inherits this task identity.
TASK_PATHS = {
    "resume_tailor": ("name", "facts.*.text"),
    "cover_letter": ("name", "contact.first_name", "facts.*.text"),
    "application_answer": ("answers.*.text", "facts.*.text"),
    "job_match": ("facts.*.text", "preferences.desired_roles"),
}
MAX_PATHS = 20
MAX_DENY_PATHS = 100
MAX_PREVIEW = 10
MAX_VALUE_CHARACTERS = 4000
MAX_CONTEXT_CHARACTERS = 32000
_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or len(path) > 256:
        raise ProfileAccessDenied("Invalid profile path")
    parts = tuple(path.split("."))
    if len(parts) > 8 or any(not _SEGMENT.fullmatch(part) for part in parts):
        raise ProfileAccessDenied("Invalid profile path")
    return parts


def _rule_matches(path: tuple[str, ...], rule: tuple[str, ...]) -> bool:
    """Allow a leaf rule or its ancestors (for metadata-only container previews)."""
    if len(path) <= len(rule) and path == rule[:len(path)]:
        return True
    return len(path) == len(rule) and all(
        expected == "*" or expected == actual
        for actual, expected in zip(path, rule)
    )


def _is_denied(path: tuple[str, ...], denied: tuple[tuple[str, ...], ...]) -> bool:
    return any(path[:len(item)] == item or item[:len(path)] == path for item in denied)


def _eligible_fact(row: dict) -> bool:
    return (row.get("verified") is True and row.get("user_approved") is True
            and type(row.get("confidence")) in (int, float)
            and row["confidence"] >= 80 and isinstance(row.get("source"), str)
            and bool(row["source"].strip()) and isinstance(row.get("text"), str)
            and bool(row["text"].strip()))


def _eligible_answer(row: dict) -> bool:
    return _eligible_fact(row)


def _selected_row(rows: list[dict], identifier: str) -> dict | None:
    matches = [row for row in rows
               if row.get("fact_id", row.get("id")) == identifier]
    return matches[0] if len(matches) == 1 else None


def _preview(value, key: str) -> dict:
    if isinstance(value, dict):
        return {"type": "object", "count": len(value),
                "preview": sorted(str(item) for item in value)[:MAX_PREVIEW]}
    if isinstance(value, list):
        if key == "facts":
            identifiers = [str(row.get("fact_id", row.get("id", "")))
                           for row in value if isinstance(row, dict)]
            return {"type": "array", "count": len(value),
                    "preview": identifiers[:MAX_PREVIEW]}
        if key == "answers":
            identifiers = [str(row.get("id", ""))
                           for row in value if isinstance(row, dict)]
            return {"type": "array", "count": len(value),
                    "preview": identifiers[:MAX_PREVIEW]}
        return {"type": "array", "count": len(value),
                "preview": list(range(min(len(value), MAX_PREVIEW)))}
    raise ProfileAccessDenied("Only profile containers support preview")


def _resolve(profile: dict, parts: tuple[str, ...]):
    value = profile
    for depth, part in enumerate(parts):
        if isinstance(value, dict):
            if part not in value:
                raise ProfileAccessDenied("Profile path is unavailable")
            value = value[part]
        elif isinstance(value, list) and depth == 1 and parts[0] in ("facts", "answers"):
            value = _selected_row(value, part)
            if value is None:
                raise ProfileAccessDenied("Profile item is unavailable")
            if parts[0] == "facts" and not _eligible_fact(value):
                raise ProfileAccessDenied("Profile item is not approved for task use")
            if parts[0] == "answers" and not _eligible_answer(value):
                raise ProfileAccessDenied("Profile item is not approved for task use")
        else:
            raise ProfileAccessDenied("Profile path is unavailable")
    if parts == ("facts",):
        return sorted((row for row in value if _eligible_fact(row)),
                      key=lambda row: row.get("fact_id", row.get("id", "")))
    if parts == ("answers",):
        return sorted((row for row in value if _eligible_answer(row)),
                      key=lambda row: row.get("id", ""))
    return value


def build_profile_context(store, profile_id: str, task: str, operation_id: str,
                          requested_paths: list[str], deny_paths: list[str] | None = None) -> dict:
    """Return a minimal task projection and audit only identifiers, never values.

    Each requested path maps to either a scalar leaf or a metadata-only
    container summary. A task may further narrow its grant with ``deny_paths``.
    Unknown tasks, denied/missing fields, and unapproved facts fail closed.
    """
    try:
        operation_id = str(UUID(operation_id))
    except (TypeError, ValueError, AttributeError) as error:
        raise ProfileAccessDenied("A valid operation identifier is required") from error
    if not isinstance(task, str) or task not in TASK_PATHS:
        _audit_denial(store, profile_id, operation_id, "UNAPPROVED", "TASK_SCOPE")
        raise ProfileAccessDenied("Profile access is not approved for this task")
    if not isinstance(requested_paths, list) or not 1 <= len(requested_paths) <= MAX_PATHS:
        raise ProfileAccessDenied("Request between 1 and 20 profile paths")
    if deny_paths is None:
        deny_paths = []
    if not isinstance(deny_paths, list) or len(deny_paths) > MAX_DENY_PATHS:
        raise ProfileAccessDenied("Invalid task denylist")
    try:
        parsed_all = [_parts(path) for path in requested_paths]
        denied = tuple(_parts(path) for path in deny_paths)
    except ProfileAccessDenied:
        _audit_denial(store, profile_id, operation_id, task, "INVALID_PATH")
        raise
    requested, parsed = [], []
    seen = set()
    for path, parts in zip(requested_paths, parsed_all):
        if path not in seen:
            requested.append(path)
            parsed.append(parts)
            seen.add(path)
    rules = tuple(tuple(rule.split(".")) for rule in TASK_PATHS[task])
    for path in parsed:
        if not any(_rule_matches(path, rule) for rule in rules):
            _audit_denial(store, profile_id, operation_id, task, "TASK_SCOPE")
            raise ProfileAccessDenied("Profile path is outside the approved task scope")
        if _is_denied(path, denied):
            _audit_denial(store, profile_id, operation_id, task, "TASK_DENYLIST")
            raise ProfileAccessDenied("Profile path is denied for this operation")

    profile = store.profile(profile_id)
    context = {}
    fact_ids, answer_ids = set(), set()
    try:
        for path, parts in zip(requested, parsed):
            value = _resolve(profile, parts)
            if isinstance(value, (dict, list)):
                value = _preview(value, parts[-1])
                if parts[0] == "facts":
                    fact_ids.update(value["preview"])
                elif parts[0] == "answers":
                    answer_ids.update(value["preview"])
            elif parts[0] == "facts":
                fact_ids.add(parts[1])
            elif parts[0] == "answers":
                answer_ids.add(parts[1])
            if isinstance(value, str) and len(value) > MAX_VALUE_CHARACTERS:
                raise ProfileAccessDenied("Profile value exceeds the task context limit")
            context[path] = value
        if len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) > MAX_CONTEXT_CHARACTERS:
            raise ProfileAccessDenied("Profile context exceeds the task context limit")
    except ProfileAccessDenied:
        _audit_denial(store, profile_id, operation_id, task, "VALUE_NOT_AVAILABLE")
        raise

    with store.engine.begin() as connection:
        store._audit(connection, profile_id, "AI_PROFILE_CONTEXT_ACCESSED", {
            "operation_id": operation_id,
            "task": task,
            "profile_version": profile["version"],
            "paths": requested,
            "fact_ids": sorted(fact_ids),
            "answer_ids": sorted(answer_ids),
        })
    return {"task": task, "operation_id": operation_id,
            "profile_version": profile["version"], "context": context,
            "accessed_fact_ids": sorted(fact_ids),
            "accessed_answer_ids": sorted(answer_ids)}


def _audit_denial(store, profile_id, operation_id, task, reason):
    # Record the denied operation without retaining requested values or secrets.
    try:
        profile_version = store.profile(profile_id)["version"]
    except Exception:
        profile_version = None
    with store.engine.begin() as connection:
        store._audit(connection, profile_id, "AI_PROFILE_CONTEXT_DENIED", {
            "operation_id": operation_id,
            "task": task,
            "profile_version": profile_version,
            # Requested paths can contain user-chosen identifiers; denied
            # operations disclose no profile data and do not need path logs.
            "paths": [],
            "reason": reason,
        })
