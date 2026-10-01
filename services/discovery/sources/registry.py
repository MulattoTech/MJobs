"""Select only explicitly supported read-only source adapters."""
from services.discovery.sources.contract import SourceAdapter
from services.discovery.sources.greenhouse import Greenhouse, SourceError


def source_adapter(source: dict, *, transport=None, check=None) -> SourceAdapter:
    if source["provider"] == "greenhouse":
        return Greenhouse(
            source["tenant"], transport=transport, check=check,
            request_timeout=source.get("request_timeout", 10),
            rate_limit=source.get("rate_limit", 60),
            concurrency=source.get("concurrency", 1),
        )
    raise SourceError("SOURCE_UNSUPPORTED")
