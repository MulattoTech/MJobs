"""Read-only, profile-independent preview of configured transient retention."""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException

from workers.transient_retention import (
    CONFIG_ENV,
    RetentionConfigError,
    parse_config,
    preview_with_config,
    read_saved_summary,
)


_SAFE_POLICY_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._*?-]{1,100}$")
_CATEGORIES = frozenset({
    "DEBUG_SCREENSHOT", "TEMP_HTML", "RAW_PAYLOAD", "BROWSER_ARTIFACT",
    "GENERATED_TEMPORARY", "ROTATED_LOG",
})


def create_transient_retention_router(*, environ=None, engine=None) -> APIRouter:
    """Mount a preview endpoint bound to local allowlisted configuration only."""
    router = APIRouter()

    @router.get("/retention/preview")
    def retention_preview():
        environment = os.environ if environ is None else environ
        raw_config = environment.get(CONFIG_ENV)
        try:
            config = parse_config(raw_config)
        except RetentionConfigError as error:
            raise HTTPException(
                status_code=503,
                detail={"code": "TRANSIENT_RETENTION_" + error.code},
            ) from None

        preview_options = {**config, "dry_run": True, "prune_enabled": False}
        preview = preview_with_config(preview_options, **({"engine": engine} if engine else {}))
        policy = []
        for rule in config["rules"]:
            rule_id = rule.get("id")
            category = rule.get("category")
            pattern = rule.get("pattern")
            age = rule.get("max_age_days")
            if (isinstance(rule_id, str) and _SAFE_POLICY_ID.fullmatch(rule_id)
                    and isinstance(category, str) and category in _CATEGORIES
                    and isinstance(pattern, str)
                    and _SAFE_PATTERN.fullmatch(pattern) and type(age) is int
                    and 1 <= age <= 365):
                policy.append({
                    "id": rule_id,
                    "category": category,
                    "pattern": pattern,
                    "max_age_days": age,
                })
        return {
            "policy": policy,
            "preview": preview,
            "last_sweep": read_saved_summary(config),
        }

    return router
