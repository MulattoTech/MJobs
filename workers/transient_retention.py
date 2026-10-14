"""Configured worker entrypoint for bounded transient-file retention."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime
import re
from pathlib import Path
import tempfile
from typing import Callable

from services.privacy.retention_policy import effective_retention_config, read_retention_policy
from services.privacy.transient_retention import _PROTECTED_COMPONENTS, run_transient_retention


CONFIG_ENV = "MJOBS_TRANSIENT_RETENTION_CONFIG"
_CATEGORIES = frozenset({
    "DEBUG_SCREENSHOT", "TEMP_HTML", "RAW_PAYLOAD", "BROWSER_ARTIFACT",
    "GENERATED_TEMPORARY", "ROTATED_LOG",
})
_RESULT_FIELDS = {
    "candidate_count", "removed_count", "candidate_bytes", "removed_bytes",
}
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_RULE_FIELDS = {"id", "category", "candidate_count", "removed_count",
                "candidate_bytes", "removed_bytes"}
_MAX_SUMMARY_BYTES = 1024 * 1024


class RetentionConfigError(ValueError):
    """A fixed-code error for missing or unsafe local retention configuration."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _reparse(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _safe_absolute(value: object) -> Path | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    try:
        path = Path(value)
        if not path.is_absolute():
            return None
        if ".." in path.parts:
            return None
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if _reparse(current):
                return None
        return path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def parse_config(value: str | Mapping | None) -> dict:
    """Parse and validate the fixed environment config without echoing values."""
    if value is None or value == "":
        raise RetentionConfigError("CONFIG_NOT_CONFIGURED")
    if isinstance(value, str):
        try:
            config = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            raise RetentionConfigError("CONFIG_INVALID") from None
    else:
        config = value
    if not isinstance(config, Mapping):
        raise RetentionConfigError("CONFIG_INVALID")
    required = {"configured_root", "allowed_roots", "rules", "summary_path"}
    permitted = required | {"dry_run", "prune_enabled"}
    if not required.issubset(config) or not set(config).issubset(permitted):
        raise RetentionConfigError("CONFIG_INVALID")

    configured_root = _safe_absolute(config.get("configured_root"))
    allowed_values = config.get("allowed_roots")
    if (configured_root is None or not isinstance(allowed_values, Sequence)
            or isinstance(allowed_values, (str, bytes)) or not allowed_values):
        raise RetentionConfigError("CONFIG_INVALID")
    allowed_roots = [_safe_absolute(item) for item in allowed_values]
    if any(item is None for item in allowed_roots):
        raise RetentionConfigError("CONFIG_INVALID")
    allowed = [item for item in allowed_roots if item is not None]
    if configured_root not in allowed or any(item != configured_root for item in allowed):
        raise RetentionConfigError("CONFIG_INVALID")

    rules = config.get("rules")
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)) or not rules:
        raise RetentionConfigError("CONFIG_INVALID")
    summary_path = _safe_absolute(config.get("summary_path"))
    # The receipt must live under an explicit allowlisted root. Individual
    # managed roots are excluded below so a cleanup rule cannot match it.
    if (summary_path is None or summary_path == configured_root
            or not any(_below(summary_path, root) for root in allowed)
            or (summary_path.exists() and summary_path.is_dir())):
        raise RetentionConfigError("CONFIG_INVALID")
    if any(part.casefold() in _PROTECTED_COMPONENTS
           for part in summary_path.relative_to(configured_root).parts):
        raise RetentionConfigError("CONFIG_INVALID")
    if any(not isinstance(rule, Mapping) for rule in rules):
        raise RetentionConfigError("CONFIG_INVALID")
    for rule in rules:
        relative = rule.get("relative_root")
        if isinstance(relative, str) and relative and "\\" not in relative:
            managed = _safe_absolute(configured_root.joinpath(*relative.split("/")))
            if managed is not None and _below(summary_path, managed):
                raise RetentionConfigError("CONFIG_INVALID")
    dry_run = config.get("dry_run", True)
    prune_enabled = config.get("prune_enabled", False)
    if type(dry_run) is not bool or type(prune_enabled) is not bool:
        raise RetentionConfigError("CONFIG_INVALID")
    return {
        "configured_root": str(configured_root),
        "allowed_roots": [str(item) for item in allowed],
        "rules": [dict(rule) for rule in rules],
        "summary_path": str(summary_path),
        "dry_run": dry_run,
        "prune_enabled": prune_enabled,
    }


def _integer(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def sanitize_summary(value: object) -> dict:
    """Return only the documented, path-free retention summary fields."""
    if not isinstance(value, Mapping):
        return _failure("INVALID_ENGINE_RESULT")
    status = value.get("status")
    dry_run = value.get("dry_run")
    if (not isinstance(status, str) or status not in {"OK", "BLOCKED", "PARTIAL"}
            or type(dry_run) is not bool):
        return _failure("INVALID_ENGINE_RESULT")
    raw_errors = value.get("errors", [])
    errors = sorted({code for code in raw_errors
                     if isinstance(code, str) and _SAFE_CODE.fullmatch(code)}) \
        if isinstance(raw_errors, Sequence) and not isinstance(raw_errors, (str, bytes)) else []
    output = {
        "status": status,
        "dry_run": dry_run,
        "candidate_count": _integer(value.get("candidate_count")),
        "removed_count": _integer(value.get("removed_count")),
        "candidate_bytes": _integer(value.get("candidate_bytes")),
        "removed_bytes": _integer(value.get("removed_bytes")),
        "errors": errors,
    }
    raw_rules = value.get("by_rule", [])
    rules = []
    if isinstance(raw_rules, Sequence) and not isinstance(raw_rules, (str, bytes)):
        for entry in raw_rules:
            category = entry.get("category") if isinstance(entry, Mapping) else None
            if (not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str)
                    or not isinstance(category, str) or category not in _CATEGORIES):
                continue
            rules.append({
                "id": entry["id"][:40],
                "category": entry["category"],
                **{field: _integer(entry.get(field)) for field in _RESULT_FIELDS},
            })
    output["by_rule"] = rules
    raw_categories = value.get("by_category", {})
    categories = {}
    if isinstance(raw_categories, Mapping):
        for category in sorted(_CATEGORIES):
            values = raw_categories.get(category, {})
            values = values if isinstance(values, Mapping) else {}
            categories[category] = {
                field: _integer(values.get(field)) for field in _RESULT_FIELDS
            }
    output["by_category"] = categories
    return output


def _failure(code: str, *, dry_run: bool = True) -> dict:
    return {
        "status": "BLOCKED",
        "dry_run": dry_run,
        "candidate_count": 0,
        "removed_count": 0,
        "candidate_bytes": 0,
        "removed_bytes": 0,
        "errors": [code],
        "by_rule": [],
        "by_category": {},
    }


def _policy_rules(config: Mapping, policy: Mapping) -> list[dict] | None:
    """Overlay persisted ages on exact server-defined rules only."""
    rules = config.get("rules")
    policy_rules = policy.get("rules")
    if (not isinstance(rules, Sequence) or isinstance(rules, (str, bytes))
            or not isinstance(policy_rules, Sequence)
            or isinstance(policy_rules, (str, bytes))):
        return None
    configured = {rule.get("id"): rule for rule in rules if isinstance(rule, Mapping)}
    if len(configured) != len(rules) or len(policy_rules) != len(configured):
        return None
    ages = {}
    for item in policy_rules:
        if not isinstance(item, Mapping):
            return None
        rule_id = item.get("id")
        rule = configured.get(rule_id)
        age = item.get("max_age_days")
        if (rule is None or item.get("category") != rule.get("category")
                or type(age) is not int or not 1 <= age <= 365 or rule_id in ages):
            return None
        ages[rule_id] = age
    if set(ages) != set(configured):
        return None
    return [{**dict(rule), "max_age_days": ages[rule["id"]]} for rule in rules]


def _load_policy(
    store,
    parsed: Mapping,
    *,
    policy_record: Mapping | None = None,
) -> dict | None:
    if store is None:
        return None
    if policy_record is not None:
        policy = policy_record
    else:
        reader = read_retention_policy
        try:
            policy = reader(store, parsed)
        except Exception:
            return {"_error": "POLICY_READ_FAILED"}
    if (not isinstance(policy, Mapping) or type(policy.get("enabled")) is not bool
            or type(policy.get("pruning_allowed")) is not bool
            or type(policy.get("requires_review")) is not bool):
        return {"_error": "POLICY_INVALID_RESULT"}
    return dict(policy)


def _effective_config(store, parsed: Mapping) -> dict | None:
    try:
        value = effective_retention_config(store, parsed)
    except Exception:
        return {"_error": "POLICY_READ_FAILED"}
    if not isinstance(value, Mapping):
        return {"_error": "POLICY_INVALID_RESULT"}
    try:
        effective = parse_config(value)
    except RetentionConfigError:
        return {"_error": "POLICY_INVALID_RESULT"}
    if (effective["configured_root"] != parsed["configured_root"]
            or effective["allowed_roots"] != parsed["allowed_roots"]
            or effective["summary_path"] != parsed["summary_path"]
            or len(effective["rules"]) != len(parsed["rules"])):
        return {"_error": "POLICY_INVALID_RESULT"}
    expected = {rule.get("id"): rule for rule in parsed["rules"]}
    observed = {rule.get("id"): rule for rule in effective["rules"]}
    if (len(expected) != len(parsed["rules"]) or len(observed) != len(effective["rules"])
            or set(expected) != set(observed)):
        return {"_error": "POLICY_INVALID_RESULT"}
    for rule_id, original in expected.items():
        current = observed[rule_id]
        if any(current.get(field) != original.get(field)
               for field in ("category", "relative_root", "pattern")):
            return {"_error": "POLICY_INVALID_RESULT"}
        age = current.get("max_age_days")
        if type(age) is not int or not 1 <= age <= 365:
            return {"_error": "POLICY_INVALID_RESULT"}
    if ((not parsed["prune_enabled"] and effective["prune_enabled"])
            or (parsed["dry_run"] and not effective["dry_run"])
            or (effective["prune_enabled"] and effective["dry_run"])):
        return {"_error": "POLICY_INVALID_RESULT"}
    return effective


def _existing_summary(path: Path) -> dict | None:
    """Recognize only bounded, complete receipts produced by this worker."""
    if _reparse(path) or not path.is_file():
        return None
    with path.open("rb") as stream:
        payload = stream.read(_MAX_SUMMARY_BYTES + 1)
    if len(payload) > _MAX_SUMMARY_BYTES:
        return None
    raw = json.loads(payload)
    summary = sanitize_summary(raw)
    if (summary["status"] not in {"OK", "PARTIAL"}
            or summary["dry_run"] or raw != summary):
        return None
    return summary


def _summary_destination_safe(path: Path) -> bool:
    if _reparse(path) or _safe_absolute(path) != path:
        return False
    try:
        return not path.exists() or _existing_summary(path) is not None
    except (OSError, ValueError, TypeError):
        return False


def _receipt_failure(summary: dict, code: str) -> dict:
    # Cleanup has already run. Keep its observed counts even if publishing
    # the receipt fails; zero removals would be a false recovery signal.
    return {**summary, "status": "PARTIAL" if summary["removed_count"] else "BLOCKED",
            "errors": sorted(set(summary["errors"]) | {code})}


def _write_summary(path: Path, summary: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(summary, sort_keys=True, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not _summary_destination_safe(path):
            raise RetentionConfigError("SUMMARY_PATH_UNSAFE")
        os.replace(temp, path)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory = os.open(path.parent, flags)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def run_with_config(
    config: str | Mapping | None,
    *,
    store=None,
    engine: Callable = run_transient_retention,
    now: datetime | None = None,
) -> dict:
    """Run once from validated config; interruption leaves no false receipt."""
    try:
        parsed = parse_config(config)
    except RetentionConfigError as error:
        return _failure(error.code)
    effective = _effective_config(store, parsed) if store is not None else None
    if effective is not None:
        if "_error" in effective:
            return _failure(effective["_error"])
        rules = effective["rules"]
        # The policy service returns the full effective server config with both
        # consent and admin ceilings already applied. Keep the worker's final
        # delete gate explicit as a second fail-closed boundary.
        may_prune = effective["prune_enabled"] and not effective["dry_run"]
    else:
        rules = parsed["rules"]
        may_prune = parsed["prune_enabled"] and not parsed["dry_run"]
    effective_dry_run = not may_prune
    path = Path(parsed["summary_path"])
    if may_prune and not _summary_destination_safe(path):
        return _failure("SUMMARY_PATH_UNSAFE", dry_run=False)
    try:
        result = engine(
            parsed["configured_root"], rules,
            allowed_roots=parsed["allowed_roots"],
            now=now, dry_run=effective_dry_run,
        )
    except Exception:
        return _failure("SWEEP_FAILED", dry_run=effective_dry_run)
    summary = sanitize_summary(result)
    if may_prune and summary["status"] == "OK":
        if not _summary_destination_safe(path):
            return _receipt_failure(summary, "SUMMARY_PATH_UNSAFE")
        try:
            _write_summary(path, summary)
        except RetentionConfigError as error:
            return _receipt_failure(summary, error.code)
        except OSError:
            return _receipt_failure(summary, "SUMMARY_WRITE_FAILED")
    return summary


def preview_with_config(
    config: str | Mapping,
    *,
    store=None,
    policy_record: Mapping | None = None,
    engine: Callable = run_transient_retention,
) -> dict:
    """Run a read-only preview regardless of the configured prune enablement."""
    try:
        parsed = parse_config(config)
    except RetentionConfigError as error:
        return _failure(error.code)
    policy = _load_policy(store, parsed, policy_record=policy_record)
    if policy is not None:
        if "_error" in policy:
            return _failure(policy["_error"])
        rules = _policy_rules(parsed, policy)
        if rules is None:
            return _failure("POLICY_INVALID_RESULT")
        parsed = {**parsed, "rules": rules}
    try:
        result = engine(
            parsed["configured_root"], parsed["rules"],
            allowed_roots=parsed["allowed_roots"], dry_run=True,
        )
    except Exception:
        return _failure("PREVIEW_FAILED")
    return sanitize_summary(result)


def read_saved_summary(config: str | Mapping) -> dict | None:
    """Read only the fixed, allowlisted atomic receipt; malformed data is hidden."""
    try:
        parsed = parse_config(config)
        path = Path(parsed["summary_path"])
        if _reparse(path) or not path.is_file():
            return None
        return _existing_summary(path)
    except (OSError, ValueError, TypeError):
        return None


def run_from_environment(environ: Mapping | None = None, *, output=None, store=None) -> int:
    """Print a sanitized summary and return a local process status."""
    environment = os.environ if environ is None else environ
    config = environment.get(CONFIG_ENV)
    summary = run_with_config(config, store=store)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False), file=output)
    return 0 if summary["status"] == "OK" else 1


def main() -> int:
    return run_from_environment()


if __name__ == "__main__":
    raise SystemExit(main())
