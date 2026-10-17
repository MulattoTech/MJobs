"""Canonical registry for supported, review-first ATS application adapters."""
from __future__ import annotations

from collections.abc import Mapping

from services.applications.adapters.ashby import AshbyApplicationAdapter
from services.applications.adapters.bamboohr import BambooHRApplicationAdapter
from services.applications.adapters.generic_html import GenericHTMLApplicationAdapter
from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.adapters.lever import LeverApplicationAdapter
from services.applications.adapters.recruitee import RecruiteeApplicationAdapter
from services.applications.adapters.smartrecruiters import SmartRecruitersApplicationAdapter
from services.applications.adapters.teamtailor import TeamtailorApplicationAdapter
from services.applications.adapters.workable import WorkableApplicationAdapter
from services.applications.adapters.workday import WorkdayApplicationAdapter


_ADAPTERS = {
    "ashby": AshbyApplicationAdapter,
    "bamboohr": BambooHRApplicationAdapter,
    "generic_html": GenericHTMLApplicationAdapter,
    "greenhouse": GreenhouseApplicationAdapter,
    "lever": LeverApplicationAdapter,
    "recruitee": RecruiteeApplicationAdapter,
    "smartrecruiters": SmartRecruitersApplicationAdapter,
    "teamtailor": TeamtailorApplicationAdapter,
    "workable": WorkableApplicationAdapter,
    "workday": WorkdayApplicationAdapter,
}


def _provider_id(provider: object) -> str | None:
    """Return a canonical identifier without hashing untrusted values."""
    return provider.casefold() if isinstance(provider, str) else None


def providers() -> tuple[str, ...]:
    """List the provider identifiers accepted by this registry."""
    return tuple(_ADAPTERS)


def get_provider(provider: object):
    """Return a fresh adapter for a supported identifier, or ``None``."""
    provider_id = _provider_id(provider)
    adapter_type = _ADAPTERS.get(provider_id) if provider_id is not None else None
    return adapter_type() if adapter_type is not None else None


def normalize_route(provider: object, route: object) -> dict | None:
    """Validate provider identity and delegate the intact route to its adapter.

    Tenant and requisition metadata, as well as the original URL/host, are
    passed through for provider-specific validation and normalization.
    """
    provider_id = _provider_id(provider)
    if provider_id is None or not isinstance(route, Mapping):
        return None
    adapter = get_provider(provider_id)
    if adapter is None:
        return None
    route_provider = _provider_id(route.get("provider"))
    if route_provider != provider_id:
        return None
    try:
        adapter_route = dict(route)
    except (TypeError, ValueError):
        return None
    # Some adapters require the canonical spelling; the case-insensitive
    # identity comparison above still ensures it matches the selected adapter.
    adapter_route["provider"] = provider_id
    return adapter.normalize_route(adapter_route)


def capabilities(provider: object) -> dict[str, bool] | None:
    """Return a copy of the selected adapter's declared capability mapping."""
    adapter = get_provider(provider)
    if adapter is None:
        return None
    declared = adapter.capabilities()
    return dict(declared) if isinstance(declared, Mapping) else None
