"""Profile-owned opt-in for bounded employer verification; no network authority by itself."""
from __future__ import annotations

import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

DISABLED = "DISABLED"
GREENHOUSE_RECIPROCAL = "GREENHOUSE_RECIPROCAL_V1"
_INVALID = "EMPLOYER_VERIFICATION_AUTHORIZATION_INVALID"
_CHANGED = "EMPLOYER_VERIFICATION_ACTIVATION_ID_CHANGED"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_VERSION = re.compile(r"[0-9a-f]{64}\Z")


class EmployerVerificationPreference(BaseModel):
    """An explicit user preference; only persistence may mint an activation ID."""

    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[1] = 1
    mode: Literal["DISABLED", "GREENHOUSE_RECIPROCAL_V1"] = DISABLED
    activation_id: str | None = None

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError(_INVALID)
        return value

    @field_validator("activation_id", mode="before")
    @classmethod
    def exact_activation_id(cls, value):
        if value is not None and (type(value) is not str or not _ID.fullmatch(value)):
            raise ValueError(_INVALID)
        return value


def disabled_preference() -> dict:
    """Return a fresh disabled value, never a shared mutable grant."""
    return EmployerVerificationPreference().model_dump()


def normalize_activation(value, previous=None, *, company_discovery_enabled: bool) -> dict:
    """Normalize one explicit save against the server's current saved preference.

    A committed disable clears authority. A later enable mints a new ID; supplied
    historical or foreign IDs cannot activate a disabled profile. Existing active
    saves preserve the server ID, including older clients that echo a null ID.
    Revocation remains available if an old stored grant is malformed.
    """
    if type(company_discovery_enabled) is not bool:
        raise ValueError(_INVALID)
    try:
        incoming = EmployerVerificationPreference.model_validate(value)
    except (ValueError, TypeError):
        raise ValueError(_INVALID) from None
    if not company_discovery_enabled or incoming.mode == DISABLED:
        return disabled_preference()
    try:
        old = EmployerVerificationPreference.model_validate(
            disabled_preference() if previous is None else previous
        )
    except (ValueError, TypeError):
        raise ValueError(_INVALID) from None
    if old.mode == GREENHOUSE_RECIPROCAL:
        if old.activation_id is None:
            raise ValueError(_INVALID)
        if incoming.activation_id not in (None, old.activation_id):
            raise ValueError(_CHANGED)
        activation_id = old.activation_id
    else:
        if incoming.activation_id is not None:
            raise ValueError(_CHANGED)
        activation_id = uuid4().hex
    return EmployerVerificationPreference(
        mode=GREENHOUSE_RECIPROCAL, activation_id=activation_id
    ).model_dump()


def verification_policy_snapshot(profile: dict) -> dict | None:
    """Project a saved active preference; this does not authorize or dispatch I/O.

    Callers implementing the versioned bootstrap must independently bind this
    value to current persisted request/source/profile authority. In particular,
    this pure projection does not provide the required transactional write fence.
    """
    try:
        preferences = profile.get("preferences") or {}
        discovery = preferences.get("company_discovery") or {}
        enabled = discovery.get("company_discovery_enabled", True)
        if type(enabled) is not bool:
            raise ValueError(_INVALID)
        value = EmployerVerificationPreference.model_validate(
            discovery.get("employer_verification", disabled_preference())
        )
        if not enabled or value.mode == DISABLED:
            return None
        profile_id, version = profile["id"], profile["version"]
        if (value.activation_id is None
                or type(profile_id) is not str or not _PROFILE.fullmatch(profile_id)
                or type(version) is not str or not _VERSION.fullmatch(version)):
            raise ValueError(_INVALID)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError(_INVALID) from None
    return {
        "schema": "EMPLOYER_VERIFICATION_POLICY_V1",
        "version": 1,
        "mode": GREENHOUSE_RECIPROCAL,
        "profile_id": profile_id,
        "profile_version": version,
        "activation_id": value.activation_id,
        "native_provider": "greenhouse",
        "origin_scope": "PERSISTED_HINT_AND_RECIPROCAL_EMPLOYER",
        "rate_scope": "ONE_DISCOVERY_ITEM_SHARED_NATIVE_EMPLOYER",
        "request_timeout": 10,
        "rate_limit": 30,
        "concurrency": 1,
    }
