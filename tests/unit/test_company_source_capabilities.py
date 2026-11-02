"""Complete declarations without widening source authorization or calling providers."""
from copy import deepcopy
import unittest

from services.company_discovery.capabilities import company_source_capabilities, feed_capabilities
from services.discovery.sources.contract import CollectionStrategy, SourceCapabilities, SourceCapability
from services.discovery.sources.registry import DEFAULT_SOURCE_ADAPTER_REGISTRY, source_adapter


class CompanySourceCapabilityTests(unittest.TestCase):
    def assert_declaration(self, declaration):
        flags = {feature.value.casefold() for feature in SourceCapability}
        self.assertEqual(set(declaration), flags | {"strategy", "capabilities"})
        self.assertIn(declaration["strategy"], {strategy.value for strategy in CollectionStrategy})
        self.assertTrue(all(type(declaration[flag]) is bool for flag in flags))
        self.assertEqual(declaration["capabilities"], sorted(
            feature.value for feature in SourceCapability if declaration[feature.value.casefold()]))

    def test_feed_declarations_include_supported_and_unsupported_flags(self):
        for strategy, expected in (("RSS", "FEED"), ("FEED", "FEED"), ("PUBLIC_JSON", "PUBLIC_JSON")):
            with self.subTest(strategy=strategy):
                declaration = feed_capabilities(strategy).as_dict()
                self.assert_declaration(declaration)
                self.assertEqual(declaration["strategy"], expected)
                self.assertEqual(set(declaration["capabilities"]), {
                    "DISCOVERY", "JOB_DETAIL", "SALARY", "REMOTE_METADATA", "APPLICATION_REDIRECT"})
                self.assertFalse(declaration["direct_application"])
                self.assertFalse(declaration["application_status"])
        for strategy in (None, [], {}, 1, "API", "PUBLIC_HTML", "UNKNOWN"):
            with self.subTest(unsupported=strategy):
                declaration = feed_capabilities(strategy).as_dict()
                self.assert_declaration(declaration)
                self.assertEqual(declaration["strategy"], "DISABLED")
                self.assertEqual(declaration["capabilities"], [])

    def test_every_registered_collector_preserves_its_own_declaration(self):
        providers = set(DEFAULT_SOURCE_ADAPTER_REGISTRY.providers())
        self.assertEqual(providers, {"greenhouse", "lever", "remotive", "remoteok", "wwr"})
        for provider in sorted(providers):
            with self.subTest(provider=provider):
                tenant = "global:acme" if provider == "lever" else "acme"
                declaration = source_adapter({"provider": provider, "tenant": tenant}).capabilities().as_dict()
                collector = dict(id="collector", provider=provider, tenant=tenant, enabled=False,
                                 strategy=declaration["strategy"], capabilities=declaration)
                source = dict(provider=provider, tenant=tenant, collector_source_id="collector",
                              collection_strategy=declaration["strategy"], enabled=False)
                actual = company_source_capabilities(source, collector).as_dict()
                self.assert_declaration(actual)
                self.assertEqual(actual, declaration)
                if provider == "wwr":
                    self.assertFalse(actual["salary"])

    def test_malformed_or_mismatched_link_never_falls_back_to_generic_feed_support(self):
        source = dict(provider="rss", tenant="legacy", collector_source_id="collector",
                      collection_strategy="PUBLIC_JSON")
        declaration = feed_capabilities("PUBLIC_JSON").as_dict()
        collector = dict(id="collector", provider="rss", tenant="legacy",
                         strategy="PUBLIC_JSON", capabilities=declaration)
        broken = [None, [], {}, {**declaration, "salary": 1}, {**declaration, "salary": "true"},
                  {**declaration, "capabilities": ["DISCOVERY", "DISCOVERY"]},
                  {**declaration, "capabilities": [{}]}, {**declaration, "capabilities": [[]]},
                  {**declaration, "capabilities": ["INVENTED"]}, {**declaration, "strategy": "UNKNOWN"},
                  {**declaration, "direct_application": True}, {**declaration, "private": "not public"}]
        for value in broken:
            with self.subTest(declaration=value):
                self.assertEqual(company_source_capabilities(
                    source, {**collector, "capabilities": value}).as_dict(), feed_capabilities(None).as_dict())
        for key, value in (("id", "other"), ("provider", "other"), ("tenant", "other"), ("strategy", "FEED")):
            with self.subTest(binding=key):
                self.assertEqual(company_source_capabilities(
                    source, {**collector, key: value}).features, frozenset())
        self.assertEqual(company_source_capabilities(source).features, frozenset())
        self.assertEqual(company_source_capabilities({**source, "collection_strategy": "RSS"}, collector).features,
                         frozenset())

    def test_saved_extension_support_is_not_inferred_from_provider_name(self):
        declared = SourceCapabilities(CollectionStrategy.PUBLIC_HTML, frozenset({SourceCapability.DISCOVERY}))
        collector = dict(id="extension", provider="trusted_extension", tenant="acme",
                         strategy="PUBLIC_HTML", capabilities=declared.as_dict())
        source = dict(provider="trusted_extension", tenant="acme", collector_source_id="extension",
                      collection_strategy="PUBLIC_HTML")
        self.assertEqual(company_source_capabilities(source, collector), declared)
        copied = deepcopy(declared.as_dict())
        copied["discovery"] = False
        self.assertEqual(company_source_capabilities(source, {**collector, "capabilities": copied}).features,
                         frozenset())

    def test_fixture_declaration_requires_exact_fixture_source_type(self):
        fixture = company_source_capabilities({'provider':'mock-ats', 'collection_strategy':'FIXTURE'}).as_dict()
        self.assert_declaration(fixture)
        self.assertEqual(fixture['strategy'], 'API')
        self.assertTrue(fixture['discovery'])
        self.assertFalse(fixture['direct_application'])
        self.assertFalse(fixture['application_status'])
        for source in ({'provider':'mock-ats', 'collection_strategy':'API'},
                       {'provider':'unknown', 'collection_strategy':'FIXTURE'}):
            self.assertEqual(company_source_capabilities(source).features, frozenset())
