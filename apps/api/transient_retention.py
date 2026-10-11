"""Local, fixed-scope retention preview and persisted policy endpoints."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence

from fastapi import APIRouter, HTTPException, Request

from services.privacy.retention_policy import read_retention_policy, save_retention_policy
from workers.transient_retention import (
    CONFIG_ENV,
    RetentionConfigError,
    parse_config,
    preview_with_config,
    read_saved_summary,
)


_SAFE_POLICY_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._*?-]{1,100}$")
_SAFE_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_CATEGORIES = frozenset({
    "DEBUG_SCREENSHOT", "TEMP_HTML", "RAW_PAYLOAD", "BROWSER_ARTIFACT",
    "GENERATED_TEMPORARY", "ROTATED_LOG",
})
_ERROR_STATUS = {
    "RETENTION_POLICY_INVALID_CONFIG": 503,
    "RETENTION_POLICY_REVISION_CONFLICT": 409,
    "RETENTION_POLICY_CONFIG_CHANGED": 409,
    "RETENTION_POLICY_INVALID_SETTINGS": 422,
    "RETENTION_POLICY_ADMIN_DISABLED": 403,
    "RETENTION_POLICY_STORE_UNAVAILABLE": 503,
    "RETENTION_POLICY_INVALID_RESULT": 503,
    "RETENTION_POLICY_READ_FAILED": 503,
    "RETENTION_POLICY_STORAGE_FAILED": 503,
}


def _raise_policy_error(code: str) -> None:
    safe_code = code if code in _ERROR_STATUS else "RETENTION_POLICY_INVALID_SETTINGS"
    raise HTTPException(
        status_code=_ERROR_STATUS[safe_code], detail={"code": safe_code},
    ) from None


def _config_from_environment(environment: Mapping) -> dict:
    try:
        return parse_config(environment.get(CONFIG_ENV))
    except RetentionConfigError:
        _raise_policy_error("RETENTION_POLICY_INVALID_CONFIG")


def _public_policy(value: object) -> dict:
    """Validate and project only the documented path-free policy contract."""
    if not isinstance(value, Mapping):
        _raise_policy_error("RETENTION_POLICY_INVALID_RESULT")
    revision = value.get("revision")
    fingerprint = value.get("config_fingerprint")
    enabled = value.get("enabled")
    pruning_allowed = value.get("pruning_allowed")
    requires_review = value.get("requires_review")
    rules = value.get("rules")
    if (type(revision) is not int or revision < 0
            or not isinstance(fingerprint, str) or not _SAFE_FINGERPRINT.fullmatch(fingerprint)
            or type(enabled) is not bool or type(pruning_allowed) is not bool
            or type(requires_review) is not bool
            or not isinstance(rules, Sequence) or isinstance(rules, (str, bytes))):
        _raise_policy_error("RETENTION_POLICY_INVALID_RESULT")
    projected_rules = []
    seen = set()
    for rule in rules:
        if not isinstance(rule, Mapping):
            _raise_policy_error("RETENTION_POLICY_INVALID_RESULT")
        rule_id = rule.get("id")
        category = rule.get("category")
        age = rule.get("max_age_days")
        if (not isinstance(rule_id, str) or not _SAFE_POLICY_ID.fullmatch(rule_id)
                or rule_id in seen or not isinstance(category, str) or category not in _CATEGORIES
                or type(age) is not int or not 1 <= age <= 365):
            _raise_policy_error("RETENTION_POLICY_INVALID_RESULT")
        seen.add(rule_id)
        projected_rules.append({"id": rule_id, "category": category, "max_age_days": age})
    return {
        "revision": revision,
        "config_fingerprint": fingerprint,
        "enabled": enabled,
        "pruning_allowed": pruning_allowed,
        "rules": projected_rules,
        "requires_review": requires_review,
    }


def _require_store(store):
    if store is None:
        _raise_policy_error("RETENTION_POLICY_STORE_UNAVAILABLE")


def _read_policy(store, config: Mapping) -> dict:
    try:
        return _public_policy(read_retention_policy(store, config))
    except HTTPException:
        raise
    except Exception as error:
        _raise_policy_error(getattr(error, "code", "RETENTION_POLICY_READ_FAILED"))


def _parse_update(payload: object) -> tuple[int, str, bool, dict[str, int]]:
    if not isinstance(payload, dict) or set(payload) != {
            "expected_revision", "expected_config_fingerprint", "enabled",
            "max_age_days_by_rule", "confirmed"}:
        _raise_policy_error("RETENTION_POLICY_INVALID_SETTINGS")
    revision = payload["expected_revision"]
    fingerprint = payload["expected_config_fingerprint"]
    enabled = payload["enabled"]
    confirmed = payload["confirmed"]
    ages = payload["max_age_days_by_rule"]
    if (type(revision) is not int or revision < 0
            or not isinstance(fingerprint, str) or not _SAFE_FINGERPRINT.fullmatch(fingerprint)
            or type(enabled) is not bool
            or confirmed is not True or not isinstance(ages, dict)):
        _raise_policy_error("RETENTION_POLICY_INVALID_SETTINGS")
    if any(not isinstance(rule_id, str) or not _SAFE_POLICY_ID.fullmatch(rule_id)
           or type(age) is not int or not 1 <= age <= 365
           for rule_id, age in ages.items()):
        _raise_policy_error("RETENTION_POLICY_INVALID_SETTINGS")
    return revision, fingerprint, enabled, dict(ages)


def create_transient_retention_router(*, store=None, environ=None, engine=None) -> APIRouter:
    """Create safe preview and policy routes using only fixed server config."""
    router = APIRouter()

    def environment():
        return os.environ if environ is None else environ

    @router.get("/retention/policy")
    def retention_policy():
        _require_store(store)
        config = _config_from_environment(environment())
        return _read_policy(store, config)

    @router.put("/retention/policy")
    async def save_policy(request: Request):
        _require_store(store)
        config = _config_from_environment(environment())
        try:
            payload = await request.json()
        except Exception:
            _raise_policy_error("RETENTION_POLICY_INVALID_SETTINGS")
        expected_revision, expected_fingerprint, enabled, ages = _parse_update(payload)
        try:
            saved = save_retention_policy(
                store, config, expected_revision=expected_revision,
                expected_config_fingerprint=expected_fingerprint,
                enabled=enabled, max_age_days_by_rule=ages,
            )
        except Exception as error:
            _raise_policy_error(getattr(error, "code", "RETENTION_POLICY_INVALID_SETTINGS"))
        return _public_policy(saved)

    @router.get("/retention/preview")
    def retention_preview():
        env = environment()
        try:
            config = parse_config(env.get(CONFIG_ENV))
        except RetentionConfigError as error:
            raise HTTPException(
                status_code=503,
                detail={"code": "TRANSIENT_RETENTION_" + error.code},
            ) from None

        policy = _read_policy(store, config) if store is not None else None
        preview_options = {**config, "dry_run": True, "prune_enabled": False}
        options = {"engine": engine} if engine is not None else {}
        if store is not None:
            options["store"] = store
            options["policy_record"] = policy
        preview = preview_with_config(preview_options, **options)
        policy_ages = ({rule["id"]: rule["max_age_days"] for rule in policy["rules"]}
                       if policy is not None else {})
        public_rules = []
        for rule in config["rules"]:
            rule_id = rule.get("id")
            category = rule.get("category")
            pattern = rule.get("pattern")
            age = policy_ages.get(rule_id, rule.get("max_age_days"))
            if (isinstance(rule_id, str) and _SAFE_POLICY_ID.fullmatch(rule_id)
                    and isinstance(category, str) and category in _CATEGORIES
                    and isinstance(pattern, str)
                    and _SAFE_PATTERN.fullmatch(pattern) and type(age) is int
                    and 1 <= age <= 365):
                public_rules.append({
                    "id": rule_id,
                    "category": category,
                    "pattern": pattern,
                    "max_age_days": age,
                })
        return {
            "policy": public_rules,
            "preview": preview,
            "last_sweep": read_saved_summary(config),
        }

    return router
