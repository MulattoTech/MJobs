"""Declarations for existing company-source consumers, independent of authorization."""
from services.discovery.sources.contract import (
    CollectionStrategy, SourceCapabilities, SourceCapability,
)


UNSUPPORTED = SourceCapabilities(CollectionStrategy.DISABLED, frozenset())
FEED_FEATURES = frozenset({
    SourceCapability.DISCOVERY, SourceCapability.JOB_DETAIL,
    SourceCapability.SALARY, SourceCapability.REMOTE_METADATA,
    SourceCapability.APPLICATION_REDIRECT,
})


def feed_capabilities(strategy):
    """The importer reads supplied content/metadata/links; it cannot submit or track applications."""
    if not isinstance(strategy, str):
        return UNSUPPORTED
    declared = {"RSS": CollectionStrategy.FEED, "FEED": CollectionStrategy.FEED,
                "PUBLIC_JSON": CollectionStrategy.PUBLIC_JSON}.get(strategy)
    return SourceCapabilities(declared, FEED_FEATURES) if declared else UNSUPPORTED


def fixture_source_capabilities():
    """Local mock-ATS discovery reads API job data; application workflows remain separate."""
    return SourceCapabilities(CollectionStrategy.API, FEED_FEATURES)


def _stored_declaration(value):
    """Accept only a complete, internally consistent durable adapter declaration."""
    if (not isinstance(value, dict) or not isinstance(value.get("capabilities"), list)
            or any(not isinstance(feature, str) for feature in value["capabilities"])):
        return UNSUPPORTED
    try:
        result = SourceCapabilities(CollectionStrategy(value["strategy"]), frozenset(
            SourceCapability(feature) for feature in value["capabilities"]))
    except (KeyError, TypeError, ValueError):
        return UNSUPPORTED
    if any(type(value.get(feature.value.casefold())) is not bool for feature in SourceCapability):
        return UNSUPPORTED
    return result if value == result.as_dict() else UNSUPPORTED


def company_source_capabilities(source, collector=None):
    """Project the actual linked collector or feed consumer, never grant collection authority."""
    if source.get("collector_source_id"):
        if (collector is None or collector.get("id") != source["collector_source_id"]
                or any(collector.get(key) != source.get(key) for key in ("provider", "tenant"))):
            return UNSUPPORTED
        declaration = _stored_declaration(collector.get("capabilities"))
        if (collector.get("strategy") != declaration.strategy.value
                or source.get("collection_strategy") != declaration.strategy.value):
            return UNSUPPORTED
        return declaration
    if source.get("provider") in {"rss", "json_feed"}:
        # Legacy imports can have a distinct tenant or an RSS-labelled JSON feed.
        # Monitoring eligibility remains a separate, stricter admission decision.
        return feed_capabilities(source.get("collection_strategy"))
    if source.get("provider") == "mock-ats" and source.get("collection_strategy") == "FIXTURE":
        return fixture_source_capabilities()
    return UNSUPPORTED
