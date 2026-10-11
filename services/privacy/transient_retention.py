"""Explicitly allowlisted, age-bounded retention for transient files only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fnmatch
import os
from pathlib import Path
import re
from collections.abc import Mapping, Sequence


MAX_RETENTION_DAYS = 365
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._*?-]{1,100}$")
_SAFE_RULE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_CATEGORIES = (
    "DEBUG_SCREENSHOT", "TEMP_HTML", "RAW_PAYLOAD", "BROWSER_ARTIFACT",
    "GENERATED_TEMPORARY", "ROTATED_LOG",
)
_PROTECTED_COMPONENTS = frozenset({
    "applications", "application", "history", "resumes", "resume", "documents",
    "uploads", "profiles", "credentials", "credential", "secrets", "secret",
    "jobs", "sources", "source", "database", "db", "tokens", "sessions",
})

def _is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _safe_absolute(path: object) -> Path | None:
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            return None
        # Check original components before resolving so a symlinked ancestor
        # cannot disappear from the inspection path.
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current = current / part
            if _is_reparse_point(current):
                return None
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _valid_rule(rule: object) -> tuple[str, str, str, str, int] | None:
    if not isinstance(rule, Mapping) or set(rule) != {
            "id", "category", "relative_root", "pattern", "max_age_days"}:
        return None
    rule_id = rule.get("id")
    category = rule.get("category")
    relative = rule.get("relative_root")
    pattern = rule.get("pattern")
    max_age_days = rule.get("max_age_days")
    if (not isinstance(rule_id, str) or not _SAFE_RULE_ID.fullmatch(rule_id)
            or not isinstance(category, str) or category not in _CATEGORIES
            or not isinstance(relative, str) or not relative or "\\" in relative
            or relative.startswith("/") or ":" in relative):
        return None
    parts = relative.split("/")
    if any(part in ("", ".", "..") or not _SAFE_COMPONENT.fullmatch(part) for part in parts):
        return None
    if any(part.casefold() in _PROTECTED_COMPONENTS for part in parts):
        return None
    if (not isinstance(pattern, str) or not _SAFE_PATTERN.fullmatch(pattern)
            or pattern in ("*", "*.*", ".*")):
        return None
    if type(max_age_days) is not int or not 1 <= max_age_days <= MAX_RETENTION_DAYS:
        return None
    return rule_id, category, "/".join(parts), pattern, max_age_days


def _rule_root(root: Path, relative: str) -> Path | None:
    current = root
    for part in relative.split("/"):
        current = current / part
        if _is_reparse_point(current):
            return None
    try:
        resolved = current.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    return resolved if _below(resolved, root) else None


def _safe_filename(name: str) -> bool:
    return bool(_SAFE_COMPONENT.fullmatch(name)) and name not in {".", ".."}


def _summary(*, dry_run: bool, status: str = "OK", errors: Sequence[str] = (),
             candidate_count: int = 0, removed_count: int = 0,
             candidate_bytes: int = 0, removed_bytes: int = 0,
             by_rule: Sequence[Mapping] = ()) -> dict:
    categories = {
        category: {"candidate_count": 0, "removed_count": 0,
                   "candidate_bytes": 0, "removed_bytes": 0}
        for category in _CATEGORIES
    }
    for entry in by_rule:
        category = entry.get("category")
        if category not in categories:
            continue
        for field in ("candidate_count", "removed_count", "candidate_bytes", "removed_bytes"):
            categories[category][field] += entry.get(field, 0)
    return {
        "status": status,
        "dry_run": dry_run,
        "candidate_count": candidate_count,
        "removed_count": removed_count,
        "candidate_bytes": candidate_bytes,
        "removed_bytes": removed_bytes,
        "errors": sorted(set(errors)),
        "by_rule": [dict(entry) for entry in by_rule],
        "by_category": categories,
    }


def run_transient_retention(
    configured_root: str | Path,
    rules: Sequence[Mapping],
    *,
    allowed_roots: Sequence[str | Path],
    now: datetime | None = None,
    dry_run: bool = True,
) -> dict:
    """Preview or prune old direct-child files covered by explicit policies.

    ``configured_root`` must be equal to, or below, a configured allowlisted
    root. Each rule has an explicit ``relative_root``, category,
    ID, basename glob, and bounded age in days. Only regular direct-child files
    are eligible; directories and unknown names are never recursively removed.
    The age boundary is strict: files exactly ``max_age_days`` old are retained.
    """
    if type(dry_run) is not bool:
        return _summary(dry_run=True, status="BLOCKED", errors=("INVALID_MODE",))
    if not isinstance(allowed_roots, Sequence) or isinstance(allowed_roots, (str, bytes)):
        return _summary(dry_run=dry_run, status="BLOCKED", errors=("INVALID_ALLOWLIST",))
    root = _safe_absolute(configured_root)
    if root is None or not root.is_dir():
        return _summary(dry_run=dry_run, status="BLOCKED", errors=("UNSAFE_ROOT",))
    allowed = [_safe_absolute(value) for value in allowed_roots]
    if not allowed or any(value is None for value in allowed):
        return _summary(dry_run=dry_run, status="BLOCKED", errors=("INVALID_ALLOWLIST",))
    allowed = [value for value in allowed if value is not None]
    # The configured base itself must be within the configured allowlist.
    if not any(_below(root, value) for value in allowed):
        return _summary(dry_run=dry_run, status="BLOCKED", errors=("ROOT_NOT_ALLOWLISTED",))
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return _summary(dry_run=dry_run, status="BLOCKED", errors=("INVALID_RULE",))

    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        return _summary(dry_run=dry_run, status="BLOCKED", errors=("INVALID_TIME",))
    current = current.astimezone(timezone.utc)

    validated: list[tuple[str, str, Path, str, int]] = []
    seen_rules: set[str] = set()
    seen_roots: set[str] = set()
    errors: set[str] = set()
    for rule in rules:
        parsed = _valid_rule(rule)
        if parsed is None:
            errors.add("INVALID_RULE")
            continue
        rule_id, category, relative, pattern, max_age_days = parsed
        if rule_id in seen_rules or relative.casefold() in seen_roots:
            errors.add("INVALID_RULE")
            continue
        seen_rules.add(rule_id)
        seen_roots.add(relative.casefold())
        managed = _rule_root(root, relative)
        if managed is None:
            errors.add("UNSAFE_PATH")
            continue
        if not any(_below(managed, allowlisted) for allowlisted in allowed):
            errors.add("ROOT_NOT_ALLOWLISTED")
            continue
        validated.append((rule_id, category, managed, pattern, max_age_days))
    if errors:
        return _summary(dry_run=dry_run, status="BLOCKED", errors=errors)

    candidates: dict[Path, tuple[int, int, int, int, str, str]] = {}
    by_rule = {
        rule_id: {"id": rule_id, "category": category, "candidate_count": 0,
                  "removed_count": 0, "candidate_bytes": 0, "removed_bytes": 0}
        for rule_id, category, _managed, _pattern, _days in validated
    }
    cutoff_by_root: dict[tuple[Path, int], float] = {}
    for rule_id, category, managed, pattern, max_age_days in validated:
        if not managed.exists():
            continue
        if _is_reparse_point(managed) or not managed.is_dir():
            errors.add("UNSAFE_PATH")
            continue
        cutoff_key = (managed, max_age_days)
        cutoff = cutoff_by_root.setdefault(
            cutoff_key, (current - timedelta(days=max_age_days)).timestamp(),
        )
        try:
            children = list(managed.iterdir())
        except OSError:
            errors.add("SCAN_FAILED")
            continue
        for child in children:
            if not _safe_filename(child.name) or not fnmatch.fnmatchcase(child.name.casefold(), pattern.casefold()):
                continue
            if _is_reparse_point(child):
                errors.add("UNSAFE_PATH")
                continue
            try:
                stat = child.stat(follow_symlinks=False)
                if not child.is_file() or stat.st_mtime >= cutoff:
                    continue
                resolved = child.resolve(strict=True)
                if not _below(resolved, managed):
                    errors.add("UNSAFE_PATH")
                    continue
                if resolved in candidates:
                    errors.add("INVALID_RULE")
                    continue
                candidates[resolved] = (
                    stat.st_size, stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                    rule_id, category,
                )
            except OSError:
                errors.add("SCAN_FAILED")
    if errors:
        return _summary(dry_run=dry_run, status="BLOCKED", errors=errors)

    files_matched = len(candidates)
    bytes_matched = sum(value[0] for value in candidates.values())
    for size, _device, _inode, _modified, rule_id, _category in candidates.values():
        by_rule[rule_id]["candidate_count"] += 1
        by_rule[rule_id]["candidate_bytes"] += size
    if dry_run:
        return _summary(
            dry_run=True, candidate_count=files_matched, candidate_bytes=bytes_matched,
            by_rule=by_rule.values(),
        )

    files_pruned = 0
    bytes_pruned = 0
    for path, observed in candidates.items():
        size, device, inode, modified, rule_id, _category = observed
        try:
            if _is_reparse_point(path):
                errors.add("UNSAFE_PATH")
                continue
            current_stat = path.stat(follow_symlinks=False)
            if ((current_stat.st_size, current_stat.st_dev, current_stat.st_ino,
                 current_stat.st_mtime_ns) != (size, device, inode, modified) or not path.is_file()):
                errors.add("FILE_CHANGED")
                continue
            path.unlink()
            files_pruned += 1
            bytes_pruned += size
            by_rule[rule_id]["removed_count"] += 1
            by_rule[rule_id]["removed_bytes"] += size
        except OSError:
            errors.add("PRUNE_FAILED")

    status = "PARTIAL" if errors and files_pruned else "BLOCKED" if errors else "OK"
    return _summary(
        dry_run=False,
        status=status,
        errors=errors,
        candidate_count=files_matched,
        removed_count=files_pruned,
        candidate_bytes=bytes_matched,
        removed_bytes=bytes_pruned,
        by_rule=by_rule.values(),
    )
