"""Provider-neutral, opt-in search results for employer career discovery.

Search output is untrusted candidate data. Callers must still validate any URL
through the bounded company-discovery transport before making a request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CompanySearchCandidate:
    url: str
    title: str
    snippet: str
    provider: str
    query: str
    evidence: tuple[str, ...] = ("SEARCH_PROVIDER_RESULT",)


class WebDiscoveryProvider(Protocol):
    """Small adapter contract; API vendors and custom endpoints implement it."""

    provider_id: str

    def search(self, query: str, *, limit: int) -> Iterable[Mapping[str, object]]: ...


class DisabledWebDiscoveryProvider:
    """Default provider. Existing supplied-site discovery works without search."""

    provider_id = "disabled"

    def search(self, query: str, *, limit: int) -> tuple[()]:
        return ()


def _candidate_url(value: object) -> str | None:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return raw


def search_company_careers(company_name: str, provider: WebDiscoveryProvider | None = None,
                           *, limit: int = 5) -> tuple[CompanySearchCandidate, ...]:
    """Return bounded, provenance-tagged candidates from an optional provider."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("COMPANY_SEARCH_LIMIT_INVALID")
    name = " ".join(str(company_name or "").split())[:200]
    if not name:
        return ()
    selected = provider or DisabledWebDiscoveryProvider()
    search = getattr(selected, "search", None)
    provider_id = str(getattr(selected, "provider_id", "")).strip()
    if not callable(search) or not provider_id:
        raise ValueError("COMPANY_SEARCH_PROVIDER_INVALID")
    query = f"{name} careers jobs"
    raw_results = search(query, limit=limit)
    if raw_results is None or isinstance(raw_results, (str, bytes, Mapping)):
        raise ValueError("COMPANY_SEARCH_RESPONSE_INVALID")
    results = []
    for position, item in enumerate(raw_results):
        if position >= 100:
            break
        if not isinstance(item, Mapping):
            continue
        url = _candidate_url(item.get("url"))
        if not url:
            continue
        results.append(CompanySearchCandidate(
            url=url,
            title=str(item.get("title") or "")[:300],
            snippet=str(item.get("snippet") or "")[:1000],
            provider=provider_id,
            query=query,
        ))
        if len(results) == limit:
            break
    return tuple(results)
