"""Typed provider boundary and source capability declarations."""
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SourceCapability(StrEnum):
    DISCOVERY = "DISCOVERY"
    JOB_DETAIL = "JOB_DETAIL"
    SALARY = "SALARY"
    REMOTE_METADATA = "REMOTE_METADATA"
    APPLICATION_REDIRECT = "APPLICATION_REDIRECT"
    DIRECT_APPLICATION = "DIRECT_APPLICATION"
    APPLICATION_STATUS = "APPLICATION_STATUS"


class CollectionStrategy(StrEnum):
    API = "API"
    FEED = "FEED"
    PUBLIC_JSON = "PUBLIC_JSON"
    PUBLIC_HTML = "PUBLIC_HTML"
    BROWSER = "BROWSER"
    ASSISTED = "ASSISTED"
    MANUAL = "MANUAL"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class SourceCapabilities:
    strategy: CollectionStrategy
    features: frozenset[SourceCapability]

    def supports(self, capability: SourceCapability) -> bool:
        return capability in self.features

    def as_dict(self) -> dict:
        payload = {
            "strategy": self.strategy.value,
            "capabilities": sorted(capability.value for capability in self.features),
        }
        payload.update({
            capability.value.casefold(): capability in self.features
            for capability in SourceCapability
        })
        return payload


@dataclass(frozen=True)
class SourceCompany:
    identity_parts: tuple[str, ...]
    name: str
    domain: str


@dataclass(frozen=True)
class SourceItem:
    raw: dict
    company: SourceCompany


@dataclass(frozen=True)
class SearchBatch:
    endpoint: str
    items: tuple[SourceItem, ...]
    source_company: SourceCompany | None = None


class SourceAdapter(Protocol):
    def search(self) -> SearchBatch: ...
    def fetch(self, job_id: int) -> dict: ...
    def normalize(self, raw: dict) -> dict: ...
    def health(self) -> str: ...
    def capabilities(self) -> SourceCapabilities: ...
