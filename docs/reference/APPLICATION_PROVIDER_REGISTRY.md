# Maintainer reference: application provider registry

**Outcome:** `documented` — the existing architecture guide names the registry’s role and warns that membership is not submission support, but it does not describe the registry’s API, extension contract, configuration, or fail-closed behavior. This note fills that maintainer-reference gap; it does not assert a defect or acceptance.

## Source identity and scope

This reference is pinned to source commit `2f01b876762fcbdd70f11e69865a48257503af93`, full blob `c672d91386e1ace625c857586f3d47521d03046a`, at `services/applications/provider_registry.py`. Line numbers below refer to that exact blob. The module is a registry/dispatch layer: it records provider factories and delegates route normalization and capability declarations to adapter instances. It does not itself implement provider-specific URL rules, analyze forms, perform network requests, upload documents, or submit applications.

## Entry points and lifecycle

- `ApplicationAdapterRegistry(extensions=(), *, disabled=())` creates a per-runtime snapshot. It starts with eleven built-in provider factories in stable declaration order: `ashby`, `bamboohr`, `breezy`, `generic_html`, `greenhouse`, `lever`, `recruitee`, `smartrecruiters`, `teamtailor`, `workable`, and `workday` (L22–34, L48–74).
- `providers()` returns the configured IDs in insertion order, excluding disabled IDs (L80–81). `factories` exposes a read-only mapping proxy, although its stored factory callables are not wrapped (L76–78).
- `is_builtin(provider)` checks the built-in ID set after safe normalization; it does not instantiate an adapter (L83–85).
- `get(provider)` lazily invokes the selected factory. Invalid, disabled, and unknown IDs return `None`; `ImportError` from factory execution is also converted to `None`. Other factory/constructor exceptions are not caught here and propagate (L87–97).
- `normalize_route(provider, route)` rejects invalid provider IDs, non-mapping routes, missing/unavailable adapters, mismatched route-provider identity, malformed mapping conversion, and adapter results that are not mappings or do not retain the canonical provider. It copies the input mapping, writes the canonical provider ID, and delegates the actual route rules to `adapter.normalize_route` (L99–119). Adapter exceptions are not caught. For `generic_html`, if a tenant was supplied, it must be a string and exactly match the adapter-normalized tenant; omission is not rejected by this registry check (L114–118).
- `capabilities(provider)` asks the adapter for its declaration and returns a plain `dict` copy only when the result is a `Mapping`; unavailable providers and non-mapping declarations yield `None`. Exceptions raised by adapter capability code propagate (L121–126).

Module-level facades `providers`, `get_provider`, `is_builtin_provider`, `normalize_route`, and `capabilities` accept a keyword-only optional `registry`. `None` selects the default singleton. A value of the wrong registry type fails closed (`()` / `None` / `False`, as appropriate) rather than being called (L129, L153–184). The registry is an adapter lookup/normalization boundary; it is not an application execution API.

## Extension and configuration contract

An extension is a two-item tuple/list `(provider_id, factory)` where the ID is already canonical lowercase ASCII matching `[a-z][a-z0-9_]{0,63}` and the factory is callable. Invalid shape/ID/factory raises `ValueError("APPLICATION_ADAPTER_DECLARATION_INVALID")`; duplicate extension IDs raise `APPLICATION_ADAPTER_ID_DUPLICATE`; IDs colliding with a built-in or the reserved `disabled` ID raise `APPLICATION_ADAPTER_ID_RESERVED` (L11–13, L51–63). A disabled ID must also be canonical; malformed IDs raise `APPLICATION_ADAPTER_ID_INVALID`, and IDs absent from the built-in-plus-extension set raise `APPLICATION_ADAPTER_ID_UNKNOWN` (L65–74). The constructor snapshots the iterable and stores the factory mapping behind `MappingProxyType` and disabled IDs in a `frozenset`; it does not call extension factories during construction (L48–74).

`configured_application_registry(module_name=None)` selects a deployment registry. With no explicit argument it strips `MJOBS_EXTENSION_REGISTRY_MODULE`; blank selection returns the default singleton. A nonblank module name must be an ASCII dotted Python identifier of at most 255 characters. It imports that module and reads `APPLICATION_ADAPTER_REGISTRY`; import/attribute failure or an attribute that is not an `ApplicationAdapterRegistry` raises `ValueError("APPLICATION_ADAPTER_CONFIGURATION_INVALID")` (L132–150). The function reads a registry object; it does not call an attribute as a factory.

## Safe synthetic examples

Read-only lookup of a built-in adapter and its declared capabilities:

```python
from services.applications.provider_registry import capabilities, providers

assert "greenhouse" in providers()
flags = capabilities("greenhouse")
# `flags` is an adapter-declared mapping copy, or None if unavailable.
```

Isolated extension registry with a synthetic adapter; no provider I/O is implied:

```python
from services.applications.provider_registry import ApplicationAdapterRegistry

class FixtureAdapter:
    def normalize_route(self, route):
        return dict(route)

    def capabilities(self):
        return {"route_normalization": True}

registry = ApplicationAdapterRegistry([("fixture_app", FixtureAdapter)])
assert registry.providers()[-1] == "fixture_app"
```

For actual URLs, use the provider’s own established route contract and pass a route whose `provider` matches the requested provider. The registry delegates URL acceptance to the selected adapter; it does not make arbitrary URLs safe merely by being listed.

## Error and safety boundaries

The registry’s safe-ID check rejects non-strings and non-ASCII identifiers before casefolding; normalized lookup IDs must match the restricted ASCII pattern (L37–42). Extension declarations and deployment-module selectors are stricter: they must already use canonical lowercase provider IDs and syntactically valid dotted module names (L51–57, L132–149). Missing/unknown/unavailable lookup paths generally return the documented empty/`None`/`False` sentinel, while invalid constructor/configuration declarations raise `ValueError`. Do not assume every adapter failure becomes a sentinel: only `ImportError` in `get` is caught; errors from normalization and capability methods propagate (L87–126).

Adapter declarations are capability metadata, not permission to perform the declared action. The architecture explicitly states registry membership is not proof of live-provider submission support and keeps real external submission OFF (existing guide at `docs/ARCHITECTURE.md:L208-L217`). The unit contracts reinforce that adapters advertise route normalization while not claiming document upload, live form interaction, or submission (`tests/unit/test_application_adapter_contract.py:L82-L97`; `tests/unit/test_fleet_t05_registry.py:L52-L60`). Examples here are synthetic and make no network or application submission.

## Existing documentation and evidence

Existing docs already cover the high-level role and submission boundary: `docs/ARCHITECTURE.md:L206-L217` says the registry dispatches route normalization/capabilities and distinguishes it from form review, dry run, and mock execution. That is useful context but not a maintainer API guide, so `documented` is appropriate rather than `no_new_gap`.

Relevant source-level test coverage (read only for this task; tests were not run):

- `tests/unit/test_application_adapter_contract.py:L59-L80` checks built-in listing, successful route normalization, provider mismatch, HTTP rejection, and user-info rejection; L82–97 checks declared action boundaries.
- `tests/unit/test_extension_point_contracts.py:L272-L310` checks isolated extension snapshots, disabled/unknown lookup behavior, and invalid/reserved/duplicate declarations; L313–350 checks registry injection into route normalization/form analysis.
- `tests/unit/test_fleet_t05_registry.py:L31-L60` checks the provider set, dispatch only to matching provider routes, and non-submission capability declarations.

**Validation status:** source and existing docs/tests were inspected statically at the pinned commit. No tests, CI, runtime imports, or external provider behavior were run or claimed.
