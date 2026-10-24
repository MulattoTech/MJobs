"""Isolated, fail-closed extension registries for sources and ATS adapters."""
from inspect import signature
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType, SimpleNamespace

import pytest

from services.applications.provider_registry import ApplicationAdapterRegistry
from services.discovery.sources.greenhouse import SourceError
from services.discovery.sources.registry import (
    DEFAULT_SOURCE_ADAPTER_REGISTRY,
    SourceAdapterRegistry,
)
from services.applications.provider_registry import DEFAULT_APPLICATION_ADAPTER_REGISTRY


def test_registry_dependency_is_explicit_at_every_admission_and_revalidation_boundary():
    from apps.api.application_form_review import (
        _application_route,
        create_application_form_review_router,
    )
    from services.applications.form_review_providers import (
        analyze_application_form,
        normalize_application_route,
    )
    from services.discovery.sources.collect import collect_source, configure_source
    from workers.application_form_review import _validate_application, handle_form_review

    boundaries = (
        configure_source,
        collect_source,
        normalize_application_route,
        analyze_application_form,
        _validate_application,
        handle_form_review,
        _application_route,
        create_application_form_review_router,
    )
    assert all("registry" in signature(boundary).parameters for boundary in boundaries)


def test_api_admission_and_worker_revalidation_use_the_same_registry(monkeypatch):
    import apps.api.application_form_review as api
    import workers.application_form_review as worker

    registry = ApplicationAdapterRegistry([("fixture_app", lambda: object())])
    row = {
        "application_id": "application-1",
        "profile_id": "profile-1",
        "route_id": "route-1",
        "target": "REAL",
        "provider": "fixture_app",
        "tenant": "fixture",
        "requisition": "1",
        "url": "https://fixture.invalid/jobs/1",
    }

    class Result:
        def mappings(self):
            return self

        def one_or_none(self):
            return row

    connection = SimpleNamespace(execute=lambda _query: Result())
    observed = []

    def normalize(provider, url, *, route_metadata, registry=None):
        observed.append((provider, url, route_metadata, registry))
        return {"provider": provider, "url": url}

    monkeypatch.setattr(api, "normalize_application_route", normalize)
    monkeypatch.setattr(worker, "normalize_application_route", normalize)

    _, api_route = api._application_route(
        object(), "profile-1", "application-1",
        connection=connection, registry=registry,
    )
    worker_route = worker._validate_application(
        connection,
        {"kind": "APPLICATION_FORM_REVIEW", "entity_id": "application-1"},
        {
            "application_id": "application-1",
            "profile_id": "profile-1",
            "route_id": "route-1",
            "provider": "fixture_app",
            "route_url": "https://fixture.invalid/jobs/1",
        },
        registry=registry,
    )

    assert api_route == worker_route
    assert [entry[3] for entry in observed] == [registry, registry]


def test_default_registries_preserve_builtin_order_and_are_read_only():
    sources = SourceAdapterRegistry()
    applications = ApplicationAdapterRegistry()

    assert sources.providers() == (
        "lever", "greenhouse", "remotive", "wwr", "remoteok",
    )
    assert applications.providers() == (
        "ashby", "bamboohr", "breezy", "generic_html", "greenhouse",
        "lever", "recruitee", "smartrecruiters", "teamtailor", "workable",
        "workday",
    )
    assert isinstance(sources.factories, MappingProxyType)
    assert isinstance(applications.factories, MappingProxyType)


def test_source_registry_isolated_snapshot_and_fail_closed_factory_admission():
    calls = []

    def factory(source, **kwargs):
        calls.append((dict(source), kwargs))
        return object()

    extensions = [("fixture_source", factory)]
    registry = SourceAdapterRegistry(extensions)
    extensions.append(("late_source", factory))

    assert registry.providers()[-1] == "fixture_source"
    assert "late_source" not in registry.providers()
    assert "fixture_source" not in DEFAULT_SOURCE_ADAPTER_REGISTRY.providers()
    assert registry.create({"provider": "fixture_source", "tenant": "fixture"}) is not None
    assert len(calls) == 1


def test_configure_source_uses_the_supplied_registry_without_global_mutation(tmp_path):
    from packages.persistence.store import Store
    from services.discovery.sources.collect import configure_source
    from services.discovery.sources.contract import (
        CollectionStrategy,
        SourceCapabilities,
        SourceCapability,
    )

    calls = []

    class Adapter:
        def capabilities(self):
            return SourceCapabilities(
                CollectionStrategy.API,
                frozenset({SourceCapability.DISCOVERY}),
            )

    def factory(source, **kwargs):
        calls.append((dict(source), kwargs))
        return Adapter()

    store = Store(f"sqlite:///{(tmp_path / 'extensions.db').as_posix()}")
    store.migrate()
    registry = SourceAdapterRegistry([("fixture_source", factory)])

    source_id = configure_source(
        store, "fixture-tenant", provider="fixture_source", registry=registry,
    )

    assert isinstance(source_id, str)
    assert calls == [({
        "provider": "fixture_source",
        "tenant": "fixture-tenant",
        "request_timeout": 10,
        "rate_limit": 60,
        "concurrency": 1,
    }, {"transport": None, "check": None})]
    assert "fixture_source" not in DEFAULT_SOURCE_ADAPTER_REGISTRY.providers()

    blocked = SourceAdapterRegistry([("fixture_source", factory)], disabled=("fixture_source",))
    for provider in ("fixture_source", "unknown", "sourcé", "bad-id"):
        with pytest.raises(SourceError, match="SOURCE_UNSUPPORTED"):
            blocked.create({"provider": provider, "tenant": "fixture"})
    assert len(calls) == 1

    for extensions in (
        [("disabled", factory)],
        [("greenhouse", factory)],
        [("fixture_source", factory), ("fixture_source", factory)],
        [("sourcé", factory)],
        [("bad-id", factory)],
    ):
        with pytest.raises(ValueError):
            SourceAdapterRegistry(extensions)
    assert len(calls) == 1


def test_application_registry_isolated_snapshot_and_fail_closed_factory_admission():
    calls = []

    class Adapter:
        def normalize_route(self, route):
            return dict(route)

        def capabilities(self):
            return {"route_normalization": True}

    def factory():
        calls.append("called")
        return Adapter()

    extensions = [("fixture_app", factory)]
    registry = ApplicationAdapterRegistry(extensions)
    extensions.append(("late_app", factory))

    assert registry.providers()[-1] == "fixture_app"
    assert "late_app" not in registry.providers()
    assert "fixture_app" not in DEFAULT_APPLICATION_ADAPTER_REGISTRY.providers()
    assert registry.get("fixture_app") is not None
    assert len(calls) == 1

    blocked = ApplicationAdapterRegistry([("fixture_app", factory)], disabled=("fixture_app",))
    for provider in ("fixture_app", "unknown", "providér", "bad-id"):
        assert blocked.get(provider) is None
    assert len(calls) == 1

    for extensions in (
        [("disabled", factory)],
        [("greenhouse", factory)],
        [("fixture_app", factory), ("fixture_app", factory)],
        [("providér", factory)],
        [("bad-id", factory)],
    ):
        with pytest.raises(ValueError):
            ApplicationAdapterRegistry(extensions)
    assert len(calls) == 1


def test_custom_application_registry_is_used_for_route_and_read_only_form_analysis():
    from services.applications.form_review_providers import (
        analyze_application_form,
        normalize_application_route,
    )

    calls = []

    class Adapter:
        def normalize_route(self, route):
            calls.append(("normalize", dict(route)))
            return dict(route) if route.get("url") == "https://fixture.invalid/jobs/1" else None

        def analyze_form(self, html, *, route_url):
            calls.append(("analyze", html, route_url))
            return {"status": "PASS", "fields": []}

        @staticmethod
        def requires_human_review(_report):
            return False

    registry = ApplicationAdapterRegistry([("fixture_app", Adapter)])
    route = normalize_application_route(
        "fixture_app", "https://fixture.invalid/jobs/1", registry=registry,
    )
    report = analyze_application_form(
        "fixture_app", "<form></form>",
        route_url="https://fixture.invalid/jobs/1", registry=registry,
    )

    assert route == {
        "provider": "fixture_app",
        "url": "https://fixture.invalid/jobs/1",
    }
    assert report == {"status": "ANALYZED", "reason": None, "fields": []}
    assert calls[-1] == (
        "analyze", "<form></form>", "https://fixture.invalid/jobs/1",
    )


def test_optional_adapter_modules_do_not_block_registry_startup_in_subprocess():
    root = Path(__file__).resolve().parents[2]
    script = r'''
import builtins
real_import = builtins.__import__
blocked = {
    "services.discovery.sources.wwr",
    "services.applications.adapters.ashby",
}
def guarded(name, globals=None, locals=None, fromlist=(), level=0):
    if name in blocked:
        raise ImportError("optional integration unavailable")
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded
from services.discovery.sources.registry import SourceAdapterRegistry
from services.applications.provider_registry import ApplicationAdapterRegistry
import services.applications.form_review_providers
import workers.application_form_review
import apps.api.application_form_review
assert SourceAdapterRegistry().providers()[0] == "lever"
assert ApplicationAdapterRegistry().providers()[0] == "ashby"
'''
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_registry_construction_does_not_mutate_ai_provider_factory_witness():
    from services.ai.tasks import AITaskRouter

    witness = lambda _provider: None
    router = AITaskRouter(provider_factory=witness)
    SourceAdapterRegistry([("fixture_source", lambda *_args, **_kwargs: None)])
    ApplicationAdapterRegistry([("fixture_app", lambda: None)])

    assert router.provider_factory is witness
