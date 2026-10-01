"""Select only explicitly supported read-only source adapters."""
from services.discovery.sources.contract import SourceAdapter
from services.discovery.sources.greenhouse import Greenhouse, SourceError


def source_adapter(source: dict, *, transport=None, check=None) -> SourceAdapter:
    if source["provider"] == "greenhouse":
        return Greenhouse(source["tenant"], transport=transport, check=check)
    raise SourceError("SOURCE_UNSUPPORTED")
