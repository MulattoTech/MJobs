"""Resolve user-configured scoring profiles without encoding role taxonomies."""
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any


_PROFILE_FIELDS = frozenset({
    "id", "version", "schema_version", "role_family", "weights", "thresholds",
})


def _invalid_profile() -> ValueError:
    return ValueError("ROLE_SCORING_PROFILE_INVALID")


def _normalize_role_family(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise _invalid_profile()
    normalized = " ".join(value.split()).casefold()
    return normalized or None


def _normalize_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROFILE_FIELDS:
        raise _invalid_profile()
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise _invalid_profile()

    profile_id = value.get("id")
    version = value.get("version")
    role_family = value.get("role_family")
    if (not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 200
            or not isinstance(version, str) or not version.strip() or len(version) > 200
            or not isinstance(role_family, str) or not role_family.strip()
            or len(role_family.strip()) > 100):
        raise _invalid_profile()

    normalized: dict[str, Any] = {
        "id": profile_id.strip(),
        "version": version.strip(),
        "schema_version": 1,
        "role_family": " ".join(role_family.split()),
        "weights": {},
        "thresholds": {},
    }
    for field in ("weights", "thresholds"):
        values = value.get(field)
        if not isinstance(values, Mapping):
            raise _invalid_profile()
        for dimension, amount in values.items():
            if (not isinstance(dimension, str) or not dimension.strip()
                    or len(dimension.strip()) > 100
                    or isinstance(amount, bool) or not isinstance(amount, (int, float))
                    or not 0 <= amount <= 100 or not isfinite(amount)):
                raise _invalid_profile()
            key = dimension.strip()
            if key in normalized[field]:
                raise _invalid_profile()
            normalized[field][key] = float(amount)
    return normalized


@dataclass(frozen=True)
class RoleScoringProfileResolution:
    """A selected user profile plus the attribution needed to explain selection."""

    profile: dict[str, Any] | None
    requested_role_family: str | None
    selection: str

    @property
    def explanation(self) -> dict[str, Any]:
        profile = self.profile
        return {
            "requested_role_family": self.requested_role_family,
            "selected_role_family": profile["role_family"] if profile else None,
            "profile_id": profile["id"] if profile else None,
            "profile_version": profile["version"] if profile else None,
            "selection": self.selection,
        }


def resolve_role_scoring_profile(
    role_family: str | None,
    profiles_by_role_family: Mapping[str, Any] | None = None,
    *,
    default_profile: Mapping[str, Any] | None = None,
) -> RoleScoringProfileResolution:
    """Select a validated profile by normalized, data-provided role-family name.

    Profiles and their dimensions are data-driven. Unknown families use the
    supplied default profile; without one, the explicit ``LEGACY_DEFAULT``
    selection leaves the existing match scorer in control.
    """
    requested = _normalize_role_family(role_family)
    if profiles_by_role_family is None:
        configured: Mapping[str, Any] = {}
    elif isinstance(profiles_by_role_family, Mapping):
        configured = profiles_by_role_family
    else:
        raise _invalid_profile()

    normalized_catalog: dict[str, dict[str, Any]] = {}
    for family, raw_profile in configured.items():
        key = _normalize_role_family(family)
        if key is None or key in normalized_catalog:
            raise _invalid_profile()
        profile = _normalize_profile(raw_profile)
        if _normalize_role_family(profile["role_family"]) != key:
            raise _invalid_profile()
        normalized_catalog[key] = profile

    if requested is not None and requested in normalized_catalog:
        return RoleScoringProfileResolution(
            profile=normalized_catalog[requested],
            requested_role_family=requested,
            selection="ROLE_FAMILY",
        )

    if default_profile is not None:
        return RoleScoringProfileResolution(
            profile=_normalize_profile(default_profile),
            requested_role_family=requested,
            selection="DEFAULT_PROFILE",
        )

    return RoleScoringProfileResolution(
        profile=None,
        requested_role_family=requested,
        selection="LEGACY_DEFAULT",
    )
