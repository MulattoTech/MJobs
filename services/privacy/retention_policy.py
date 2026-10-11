"""Persisted, config-bound consent for bounded transient-file retention."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from sqlalchemy import select, text

from packages.persistence import models as m
from packages.persistence.store import now


_ENTITY = "SYSTEM_TRANSIENT_RETENTION"
_ACTION = "RETENTION_POLICY_SAVED"
_CATEGORIES = frozenset({"DEBUG_SCREENSHOT", "TEMP_HTML", "RAW_PAYLOAD", "BROWSER_ARTIFACT",
                         "GENERATED_TEMPORARY", "ROTATED_LOG"})
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_PATTERN = re.compile(r"^[A-Za-z0-9._*?-]{1,100}$")
_PROTECTED = {"application", "applications", "history", "resume", "resumes", "document",
              "documents", "upload", "uploads", "profile", "profiles", "credential",
              "credentials", "source", "sources", "job", "jobs", "record", "records"}
_LOCK_KEY = 5571310229431997774


class RetentionPolicyError(ValueError):
    """A stable safe error; details from config and persistence are not exposed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise RetentionPolicyError(code)


def _normalized_root(value):
    if not isinstance(value, str) or not value:
        _fail("RETENTION_POLICY_INVALID_CONFIG")
    from pathlib import Path
    path = Path(value)
    if not path.is_absolute():
        _fail("RETENTION_POLICY_INVALID_CONFIG")
    return str(path.resolve(strict=False))


def _normalize_config(config):
    if not isinstance(config, Mapping):
        _fail("RETENTION_POLICY_INVALID_CONFIG")
    try:
        configured_root = _normalized_root(config.get("configured_root"))
        allowed_raw = config.get("allowed_roots")
        if not isinstance(allowed_raw, (list, tuple)) or not allowed_raw:
            _fail("RETENTION_POLICY_INVALID_CONFIG")
        allowed_roots = sorted({_normalized_root(root) for root in allowed_raw})
        if configured_root not in allowed_roots:
            _fail("RETENTION_POLICY_INVALID_CONFIG")
        if type(config.get("prune_enabled")) is not bool:
            _fail("RETENTION_POLICY_INVALID_CONFIG")
        if type(config.get("dry_run")) is not bool:
            _fail("RETENTION_POLICY_INVALID_CONFIG")
        raw_rules = config.get("rules")
        if not isinstance(raw_rules, (list, tuple)) or not raw_rules:
            _fail("RETENTION_POLICY_INVALID_CONFIG")
        rules = []
        seen = set()
        for rule in raw_rules:
            if not isinstance(rule, Mapping) or set(rule) != {
                    "id", "category", "relative_root", "pattern", "max_age_days"}:
                _fail("RETENTION_POLICY_INVALID_CONFIG")
            rule_id = rule["id"]
            category = rule["category"]
            relative_root = rule["relative_root"]
            pattern = rule["pattern"]
            age = rule["max_age_days"]
            if (not isinstance(rule_id, str) or not _ID.fullmatch(rule_id) or rule_id in seen
                    or category not in _CATEGORIES
                    or type(age) is not int or not 1 <= age <= 365
                    or not isinstance(pattern, str) or not _PATTERN.fullmatch(pattern)
                    or pattern in {"*", "*.*", ".*"}
                    or not isinstance(relative_root, str) or not relative_root
                    or "\\" in relative_root or ":" in relative_root):
                _fail("RETENTION_POLICY_INVALID_CONFIG")
            posix = PurePosixPath(relative_root)
            parts = relative_root.split("/")
            if (posix.is_absolute() or any(part in {"", ".", ".."} for part in parts)
                    or any(part.casefold() in _PROTECTED for part in parts)):
                _fail("RETENTION_POLICY_INVALID_CONFIG")
            seen.add(rule_id)
            rules.append({"id": rule_id, "category": category, "relative_root": relative_root,
                          "pattern": pattern, "max_age_days": age})
        rules.sort(key=lambda rule: rule["id"])
        canonical = {"configured_root": configured_root, "allowed_roots": allowed_roots,
                     "rules": rules, "prune_enabled": config["prune_enabled"],
                     "dry_run": config["dry_run"]}
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                                     ensure_ascii=False).encode("utf-8")).hexdigest()
        return {"configured_root": configured_root, "allowed_roots": allowed_roots,
                "rules": rules, "fingerprint": fingerprint,
                "pruning_allowed": config["prune_enabled"] is True and config["dry_run"] is False,
                "dry_run": config["dry_run"]}
    except RetentionPolicyError:
        raise
    except Exception as error:
        raise RetentionPolicyError("RETENTION_POLICY_INVALID_CONFIG") from error


def _read_latest(connection):
    row = connection.execute(
        select(m.audit.c.id, m.audit.c.payload).where(m.audit.c.entity_id == _ENTITY, m.audit.c.action == _ACTION)
        .order_by(m.audit.c.id.desc()).limit(1)
    ).first()
    if row is None:
        return None
    audit_id, payload = row[0], row[1]
    match = re.fullmatch(r"retention-policy-v1-(\d{20})", audit_id or "")
    revision_hint = int(match.group(1)) if match else 0
    if not isinstance(payload, dict):
        return {"revision": revision_hint, "_corrupt": True}
    payload = dict(payload)
    payload["_revision_hint"] = revision_hint
    return payload


def _public(normalized, record):
    defaults = {rule["id"]: rule["max_age_days"] for rule in normalized["rules"]}
    revision = 0
    enabled = False
    requires_review = True
    ages = defaults
    if record is not None:
        candidate_revision = record.get("revision")
        candidate_ages = record.get("max_age_days_by_rule")
        valid = (record.get("schema_version") == 1
                 and type(candidate_revision) is int and candidate_revision >= 1
                 and record.get("config_fingerprint") == normalized["fingerprint"]
                 and type(record.get("enabled")) is bool
                 and isinstance(candidate_ages, dict) and set(candidate_ages) == set(defaults)
                 and all(type(age) is int and 1 <= age <= 365 for age in candidate_ages.values()))
        revision_hint = record.get("_revision_hint", 0)
        if type(candidate_revision) is int and candidate_revision >= 1:
            revision = candidate_revision
        elif type(revision_hint) is int and revision_hint >= 1:
            revision = revision_hint
        if valid:
            enabled = record["enabled"]
            requires_review = False
            ages = candidate_ages
    return {"revision": revision, "config_fingerprint": normalized["fingerprint"],
            "enabled": enabled, "pruning_allowed": normalized["pruning_allowed"],
            "rules": [{"id": rule["id"], "category": rule["category"],
                       "max_age_days": ages[rule["id"]]} for rule in normalized["rules"]],
            "requires_review": requires_review}


def read_retention_policy(store, config):
    normalized = _normalize_config(config)
    try:
        with store.engine.connect() as connection:
            record = _read_latest(connection)
        return _public(normalized, record)
    except RetentionPolicyError:
        raise
    except Exception as error:
        raise RetentionPolicyError("RETENTION_POLICY_STORAGE_FAILED") from error


def effective_retention_config(store, config):
    normalized = _normalize_config(config)
    policy = read_retention_policy(store, config)
    allowed_to_prune = policy["enabled"] and policy["pruning_allowed"] and not policy["requires_review"]
    effective = dict(config)
    ages = {rule["id"]: rule["max_age_days"] for rule in policy["rules"]}
    effective["rules"] = [dict(rule, max_age_days=ages[rule["id"]]) for rule in normalized["rules"]]
    effective["prune_enabled"] = bool(allowed_to_prune and normalized["pruning_allowed"])
    effective["dry_run"] = bool(normalized["dry_run"] or not effective["prune_enabled"])
    return effective


def _validate_save(normalized, expected_revision, enabled, ages):
    if (type(expected_revision) is not int or expected_revision < 0 or type(enabled) is not bool
            or not isinstance(ages, Mapping)):
        _fail("RETENTION_POLICY_INVALID_SETTINGS")
    expected_ids = {rule["id"] for rule in normalized["rules"]}
    if set(ages) != expected_ids or any(type(age) is not int or not 1 <= age <= 365 for age in ages.values()):
        _fail("RETENTION_POLICY_INVALID_SETTINGS")
    return {rule_id: ages[rule_id] for rule_id in sorted(expected_ids)}


def save_retention_policy(store, config, *, expected_revision, expected_config_fingerprint,
                          enabled, max_age_days_by_rule):
    normalized = _normalize_config(config)
    if expected_config_fingerprint != normalized["fingerprint"]:
        _fail("RETENTION_POLICY_CONFIG_CHANGED")
    ages = _validate_save(normalized, expected_revision, enabled, max_age_days_by_rule)
    if enabled and not normalized["pruning_allowed"]:
        _fail("RETENTION_POLICY_ADMIN_DISABLED")
    connection = store.engine.connect()
    transaction = None
    try:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            transaction = connection.get_transaction()
        elif connection.dialect.name == "postgresql":
            transaction = connection.begin()
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        else:
            _fail("RETENTION_POLICY_STORAGE_FAILED")
        latest = _read_latest(connection)
        current_revision = 0
        if latest:
            if type(latest.get("revision")) is int and latest["revision"] >= 1:
                current_revision = latest["revision"]
            elif type(latest.get("_revision_hint")) is int and latest["_revision_hint"] >= 1:
                current_revision = latest["_revision_hint"]
        if current_revision != expected_revision:
            transaction.rollback()
            transaction = None
            _fail("RETENTION_POLICY_REVISION_CONFLICT")
        revision = current_revision + 1
        payload = {"schema_version": 1, "revision": revision,
                   "config_fingerprint": normalized["fingerprint"], "enabled": enabled,
                   "max_age_days_by_rule": ages}
        connection.execute(m.audit.insert().values(
            id=f"retention-policy-v1-{revision:020d}", entity_id=_ENTITY,
            action=_ACTION, payload=payload, timestamp=now()))
        transaction.commit()
        transaction = None
        return _public(normalized, payload)
    except RetentionPolicyError:
        if transaction is not None:
            transaction.rollback()
        raise
    except Exception as error:
        if transaction is not None:
            transaction.rollback()
        raise RetentionPolicyError("RETENTION_POLICY_STORAGE_FAILED") from error
    finally:
        connection.close()
