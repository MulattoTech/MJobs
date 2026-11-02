"""Declared source strategies must agree with approved, durable configuration."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import (
    SourceCollectionContractError,
    collect_source,
    configure_source,
)
from services.discovery.sources.contract import (
    CollectionStrategy,
    SearchBatch,
    SourceCapabilities,
    SourceCapability,
    SourceCompany,
    SourceItem,
)
from services.discovery.sources.errors import SourceError
from services.discovery.sources.registry import SourceAdapterRegistry
from services.discovery.sources.remoteok import RemoteOK
from workers.operations import (
    OperationCancelled,
    OperationControlUnavailable,
    OperationTimedOut,
)


def capabilities(strategy=CollectionStrategy.API, *, direct=False):
    features = {SourceCapability.DISCOVERY}
    if direct:
        features.add(SourceCapability.DIRECT_APPLICATION)
    return SourceCapabilities(strategy, frozenset(features))


@pytest.fixture
def collector(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'source-contract.db').as_posix()}")
    store.migrate()
    state = {"capabilities": capabilities(), "searches": 0, "on_search": None}
    company = SourceCompany(("fixture-contract", "employer"), "Example", "")

    class Adapter(RemoteOK):
        def capabilities(self):
            if isinstance(state["capabilities"], Exception):
                raise state["capabilities"]
            return state["capabilities"]

        def search(self):
            state["searches"] += 1
            if state["on_search"]:
                state["on_search"]()
            return SearchBatch(
                endpoint="https://fixture.invalid/jobs",
                items=(SourceItem({
                    "id": 18, "position": "Python Engineer", "company": "Example",
                    "location": "Remote", "description": "Python SQL role",
                    "url": "https://fixture.invalid/jobs/18",
                }, company),),
            )

    registry = SourceAdapterRegistry(((
        "fixture_contract", lambda _source, **kwargs: Adapter(**kwargs),
    ),))
    source_id = configure_source(store, "employer", provider="fixture_contract", registry=registry)
    try:
        yield store, source_id, registry, state
    finally:
        store.engine.dispose()


def source_row(store, source_id):
    with store.engine.connect() as connection:
        return dict(connection.execute(select(m.job_sources).where(
            m.job_sources.c.id == source_id,
        )).mappings().one())


@pytest.mark.parametrize("declared", [
    capabilities(CollectionStrategy.BROWSER),
    capabilities(direct=True),
    None,
    SourceCapabilities("BROWSER", frozenset()),
    SourceCapabilities(CollectionStrategy.API, frozenset({"DISCOVERY"})),
])
def test_changed_declaration_is_rejected_before_egress_or_health_mutation(collector, declared):
    store, source_id, registry, state = collector
    before = source_row(store, source_id)
    state["capabilities"] = declared

    with pytest.raises(SourceError, match="^SOURCE_CHANGED$"):
        collect_source(store, source_id, None, registry=registry)

    assert state["searches"] == 0
    assert source_row(store, source_id) == before
    assert store.count("canonical_jobs") == store.count("job_postings") == 0
    assert store.count("source_quality_observations") == 0


@pytest.mark.parametrize("field", ["strategy", "capabilities", "request_timeout"])
def test_source_contract_change_during_fetch_prevents_import(collector, field):
    store, source_id, registry, state = collector
    changed = {"strategy": "BROWSER", "capabilities": capabilities(direct=True).as_dict(),
               "request_timeout": 2}[field]

    def change_source():
        with store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == source_id,
            ).values(**{field: changed}))

    state["on_search"] = change_source
    with pytest.raises(SourceError, match="^SOURCE_CHANGED$"):
        collect_source(store, source_id, None, registry=registry)

    after = source_row(store, source_id)
    assert state["searches"] == 1
    assert after[field] == changed
    assert after["consecutive_failures"] == 0
    assert after["backoff_until"] is None
    assert store.count("canonical_jobs") == store.count("job_postings") == 0
    assert store.count("source_quality_observations") == 0


def test_adapter_contract_change_during_fetch_prevents_import(collector):
    store, source_id, registry, state = collector
    state["on_search"] = lambda: state.update(capabilities=capabilities(direct=True))
    with pytest.raises(SourceError, match="^SOURCE_CHANGED$"):
        collect_source(store, source_id, None, registry=registry)
    assert store.count("canonical_jobs") == store.count("job_postings") == 0
    assert source_row(store, source_id)["consecutive_failures"] == 0


@pytest.mark.parametrize("failure", [
    RuntimeError("private-token=DECLARATION_CANARY"),
    SourceError("private-token=DECLARATION_CANARY"),
])
def test_declaration_errors_are_fixed_and_detached_before_egress(collector, failure):
    store, source_id, registry, state = collector
    before = source_row(store, source_id)
    state["capabilities"] = failure
    with pytest.raises(SourceError, match="^SOURCE_CHANGED$") as raised:
        collect_source(store, source_id, None, registry=registry)
    assert raised.value.__context__ is raised.value.__cause__ is None
    assert source_row(store, source_id) == before
    assert state["searches"] == 0


@pytest.mark.parametrize("stop", [OperationCancelled, OperationTimedOut, OperationControlUnavailable])
def test_declaration_preserves_cooperative_control_failures(collector, stop):
    store, source_id, registry, state = collector
    before = source_row(store, source_id)
    state["capabilities"] = stop()
    with pytest.raises(stop):
        collect_source(store, source_id, None, registry=registry)
    assert source_row(store, source_id) == before
    assert state["searches"] == 0


def test_older_rejected_collection_cannot_erase_newer_rate_limit(collector, monkeypatch):
    store, source_id, registry, state = collector
    current = datetime.now(UTC)
    older = (current - timedelta(seconds=301)).isoformat()
    clock = iter([older])
    monkeypatch.setattr("services.discovery.sources.collect.now",
                        lambda: next(clock, current.isoformat()))
    newer = {}

    def rate_limited():
        raise SourceError("SOURCE_RATE_LIMITED", retry_seconds=600)

    def concurrent_attempt():
        # A's simulated pause exceeds the poll interval. The configured policy
        # changes, then B really executes and durably records a provider refusal.
        configure_source(store, "employer", provider="fixture_contract", registry=registry,
                         request_timeout=2)
        state["on_search"] = rate_limited
        with pytest.raises(SourceError, match="^SOURCE_RATE_LIMITED$"):
            collect_source(store, source_id, None, registry=registry)
        newer.update(source_row(store, source_id))
        state["on_search"] = None

    state["on_search"] = concurrent_attempt
    with pytest.raises(SourceError, match="^SOURCE_CHANGED$"):
        collect_source(store, source_id, None, registry=registry)
    assert newer["health_status"] == "RATE_LIMITED"
    assert newer["consecutive_failures"] == 1
    assert newer["last_error"] == "SOURCE_RATE_LIMITED"
    assert newer["backoff_until"] > current.isoformat()
    assert source_row(store, source_id) == newer
    assert store.count("canonical_jobs") == store.count("job_postings") == 0


def test_unchanged_structured_adapter_imports_without_browser_budget(collector):
    store, source_id, registry, state = collector
    result = collect_source(store, source_id, None, registry=registry)
    assert result["strategy"] == "API"
    assert result["direct_application"] is False
    assert state["searches"] == 1
    assert store.count("canonical_jobs") == store.count("job_postings") == 1


def test_approved_browser_still_requires_page_budget_before_egress(collector):
    store, source_id, registry, state = collector
    state["capabilities"] = capabilities(CollectionStrategy.BROWSER)
    configure_source(store, "employer", provider="fixture_contract", registry=registry)
    with pytest.raises(SourceCollectionContractError) as failure:
        collect_source(store, source_id, None, registry=registry)
    assert failure.value.stop_reason == "PAGE_BUDGET_REQUIRED"
    assert state["searches"] == 0
    assert source_row(store, source_id)["consecutive_failures"] == 0
