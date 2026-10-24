"""Immutable selection of explicitly supported read-only source adapters."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib import import_module
import re
from types import MappingProxyType

from services.discovery.sources.contract import SourceAdapter
from services.discovery.sources.greenhouse import SourceError


_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RESERVED = frozenset({"disabled"})


def _factory(module_name, type_name, *, default_tenant=False):
    def create(source, *, transport=None, check=None):
        adapter_type = getattr(import_module(module_name), type_name)
        tenant = source.get("tenant", "default") if default_tenant else source["tenant"]
        return adapter_type(
            tenant, transport=transport, check=check,
            request_timeout=source.get("request_timeout", 10),
            rate_limit=source.get("rate_limit", 60),
            concurrency=source.get("concurrency", 1),
        )
    return create


_BUILTINS = (
    ("lever", _factory("services.discovery.sources.lever", "Lever")),
    ("greenhouse", _factory("services.discovery.sources.greenhouse", "Greenhouse")),
    ("remotive", _factory("services.discovery.sources.remotive", "Remotive", default_tenant=True)),
    ("wwr", _factory("services.discovery.sources.wwr", "WeWorkRemotely", default_tenant=True)),
    ("remoteok", _factory("services.discovery.sources.remoteok", "RemoteOK", default_tenant=True)),
)


def _source_id(value):
    if not isinstance(value, str) or not value.isascii() or not _ID.fullmatch(value):
        return None
    return "remoteok" if value == "remote_ok" else value


class SourceAdapterRegistry:
    """An immutable per-runtime snapshot of built-in and extension factories."""

    def __init__(self, extensions: Iterable[tuple[str, object]] = (), *, disabled=()):
        factories = dict(_BUILTINS)
        extension_ids = set()
        for entry in tuple(extensions):
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise ValueError("SOURCE_ADAPTER_DECLARATION_INVALID")
            provider, factory = entry
            provider_id = _source_id(provider)
            if provider_id is None or provider != provider_id or not callable(factory):
                raise ValueError("SOURCE_ADAPTER_DECLARATION_INVALID")
            if provider_id in extension_ids:
                raise ValueError("SOURCE_ADAPTER_ID_DUPLICATE")
            if provider_id in factories or provider_id in _RESERVED:
                raise ValueError("SOURCE_ADAPTER_ID_RESERVED")
            extension_ids.add(provider_id)
            factories[provider_id] = factory

        disabled_ids = set()
        for provider in tuple(disabled):
            provider_id = _source_id(provider)
            if provider_id is None or provider != provider_id:
                raise ValueError("SOURCE_ADAPTER_ID_INVALID")
            if provider_id not in factories:
                raise ValueError("SOURCE_ADAPTER_ID_UNKNOWN")
            disabled_ids.add(provider_id)
        self._factories = MappingProxyType(factories)
        self._disabled = frozenset(disabled_ids)

    @property
    def factories(self):
        return self._factories

    def providers(self) -> tuple[str, ...]:
        return tuple(provider for provider in self._factories if provider not in self._disabled)

    def create(self, source: object, *, transport=None, check=None) -> SourceAdapter:
        if not isinstance(source, Mapping):
            raise SourceError("SOURCE_UNSUPPORTED")
        provider_id = _source_id(source.get("provider"))
        if provider_id is None or provider_id in self._disabled:
            raise SourceError("SOURCE_UNSUPPORTED")
        factory = self._factories.get(provider_id)
        if factory is None:
            raise SourceError("SOURCE_UNSUPPORTED")
        try:
            return factory(source, transport=transport, check=check)
        except ImportError:
            raise SourceError("SOURCE_UNSUPPORTED") from None


DEFAULT_SOURCE_ADAPTER_REGISTRY = SourceAdapterRegistry()


def source_adapter(source: dict, *, transport=None, check=None,
                   registry: SourceAdapterRegistry | None = None) -> SourceAdapter:
    selected = DEFAULT_SOURCE_ADAPTER_REGISTRY if registry is None else registry
    if not isinstance(selected, SourceAdapterRegistry):
        raise SourceError("SOURCE_UNSUPPORTED")
    return selected.create(source, transport=transport, check=check)
