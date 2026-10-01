"""Provider boundary; callers persist attributed postings without parsing payloads."""
from dataclasses import dataclass
from typing import Protocol


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
    def capabilities(self) -> dict: ...
    def health(self) -> str: ...
