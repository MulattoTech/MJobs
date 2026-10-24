"""Immutable registry for supported, review-first ATS application adapters."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib import import_module
import re
from types import MappingProxyType


_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RESERVED = frozenset({"disabled"})


def _factory(module_name, type_name):
    def create():
        return getattr(import_module(module_name), type_name)()
    return create


_BUILTINS = (
    ("ashby", _factory("services.applications.adapters.ashby", "AshbyApplicationAdapter")),
    ("bamboohr", _factory("services.applications.adapters.bamboohr", "BambooHRApplicationAdapter")),
    ("breezy", _factory("services.applications.adapters.breezy", "BreezyApplicationAdapter")),
    ("generic_html", _factory("services.applications.adapters.generic_html", "GenericHTMLApplicationAdapter")),
    ("greenhouse", _factory("services.applications.adapters.greenhouse", "GreenhouseApplicationAdapter")),
    ("lever", _factory("services.applications.adapters.lever", "LeverApplicationAdapter")),
    ("recruitee", _factory("services.applications.adapters.recruitee", "RecruiteeApplicationAdapter")),
    ("smartrecruiters", _factory("services.applications.adapters.smartrecruiters", "SmartRecruitersApplicationAdapter")),
    ("teamtailor", _factory("services.applications.adapters.teamtailor", "TeamtailorApplicationAdapter")),
    ("workable", _factory("services.applications.adapters.workable", "WorkableApplicationAdapter")),
    ("workday", _factory("services.applications.adapters.workday", "WorkdayApplicationAdapter")),
)


def _provider_id(provider: object) -> str | None:
    """Return a canonical safe identifier without hashing untrusted values."""
    if not isinstance(provider, str) or not provider.isascii():
        return None
    provider_id = provider.casefold()
    return provider_id if _ID.fullmatch(provider_id) else None


class ApplicationAdapterRegistry:
    """An immutable per-runtime snapshot of built-in and extension factories."""

    def __init__(self, extensions: Iterable[tuple[str, object]] = (), *, disabled=()):
        factories = dict(_BUILTINS)
        extension_ids = set()
        for entry in tuple(extensions):
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise ValueError("APPLICATION_ADAPTER_DECLARATION_INVALID")
            provider, factory = entry
            provider_id = _provider_id(provider)
            if provider_id is None or provider != provider_id or not callable(factory):
                raise ValueError("APPLICATION_ADAPTER_DECLARATION_INVALID")
            if provider_id in extension_ids:
                raise ValueError("APPLICATION_ADAPTER_ID_DUPLICATE")
            if provider_id in factories or provider_id in _RESERVED:
                raise ValueError("APPLICATION_ADAPTER_ID_RESERVED")
            extension_ids.add(provider_id)
            factories[provider_id] = factory

        disabled_ids = set()
        for provider in tuple(disabled):
            provider_id = _provider_id(provider)
            if provider_id is None or provider != provider_id:
                raise ValueError("APPLICATION_ADAPTER_ID_INVALID")
            if provider_id not in factories:
                raise ValueError("APPLICATION_ADAPTER_ID_UNKNOWN")
            disabled_ids.add(provider_id)
        self._factories = MappingProxyType(factories)
        self._disabled = frozenset(disabled_ids)

    @property
    def factories(self):
        return self._factories

    def providers(self) -> tuple[str, ...]:
        return tuple(provider for provider in self._factories if provider not in self._disabled)

    def is_builtin(self, provider: object) -> bool:
        provider_id = _provider_id(provider)
        return provider_id in dict(_BUILTINS) if provider_id is not None else False

    def get(self, provider: object):
        provider_id = _provider_id(provider)
        if provider_id is None or provider_id in self._disabled:
            return None
        factory = self._factories.get(provider_id)
        if factory is None:
            return None
        try:
            return factory()
        except ImportError:
            return None

    def normalize_route(self, provider: object, route: object) -> dict | None:
        provider_id = _provider_id(provider)
        if provider_id is None or not isinstance(route, Mapping):
            return None
        adapter = self.get(provider_id)
        if adapter is None or _provider_id(route.get("provider")) != provider_id:
            return None
        try:
            adapter_route = dict(route)
        except (TypeError, ValueError):
            return None
        adapter_route["provider"] = provider_id
        normalized = adapter.normalize_route(adapter_route)
        if not isinstance(normalized, Mapping) or normalized.get("provider") != provider_id:
            return None
        if provider_id == "generic_html" and "tenant" in adapter_route:
            supplied_tenant = adapter_route["tenant"]
            if (not isinstance(supplied_tenant, str)
                    or supplied_tenant != normalized.get("tenant")):
                return None
        return dict(normalized)

    def capabilities(self, provider: object) -> dict[str, bool] | None:
        adapter = self.get(provider)
        if adapter is None:
            return None
        declared = adapter.capabilities()
        return dict(declared) if isinstance(declared, Mapping) else None


DEFAULT_APPLICATION_ADAPTER_REGISTRY = ApplicationAdapterRegistry()


def _registry(registry):
    return DEFAULT_APPLICATION_ADAPTER_REGISTRY if registry is None else registry


def providers(*, registry: ApplicationAdapterRegistry | None = None) -> tuple[str, ...]:
    selected = _registry(registry)
    return selected.providers() if isinstance(selected, ApplicationAdapterRegistry) else ()


def get_provider(provider: object, *, registry: ApplicationAdapterRegistry | None = None):
    selected = _registry(registry)
    return selected.get(provider) if isinstance(selected, ApplicationAdapterRegistry) else None


def is_builtin_provider(provider: object, *,
                        registry: ApplicationAdapterRegistry | None = None) -> bool:
    selected = _registry(registry)
    return (selected.is_builtin(provider)
            if isinstance(selected, ApplicationAdapterRegistry) else False)


def normalize_route(provider: object, route: object, *,
                    registry: ApplicationAdapterRegistry | None = None) -> dict | None:
    selected = _registry(registry)
    return (selected.normalize_route(provider, route)
            if isinstance(selected, ApplicationAdapterRegistry) else None)


def capabilities(provider: object, *,
                 registry: ApplicationAdapterRegistry | None = None) -> dict[str, bool] | None:
    selected = _registry(registry)
    return selected.capabilities(provider) if isinstance(selected, ApplicationAdapterRegistry) else None
