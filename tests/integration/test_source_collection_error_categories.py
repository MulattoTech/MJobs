"""Source collection persists canonical categories without provider exception text."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.contract import CollectionStrategy, SearchBatch, SourceCapabilities
from services.discovery.sources.collect import (
    SourceCollectionBoundaryError,
    collect_source,
    configure_source,
)
from services.discovery.sources.errors import SourceError
from tests.integration.test_spine import PROFILE


class _FailingAdapter:
    def __init__(self, failure):
        self.failure = failure

    def search(self):
        raise self.failure


class _EmptyAdapter:
    def search(self):
        return SearchBatch(endpoint="https://example.invalid/jobs", items=())

    def capabilities(self):
        return SourceCapabilities(strategy=CollectionStrategy.API, features=frozenset())


class SourceCollectionErrorCategoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _source(self, suffix):
        return configure_source(self.store, f"error-category-{suffix}")

    def _persisted_failure(self, source_id):
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
            observation = connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.configured_source_id == source_id)).mappings().one()
            audit = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == source_id,
                m.audit.c.action == "SOURCE_COLLECTION_FAILED")).mappings().one_or_none()
        return dict(source), dict(observation), dict(audit) if audit else None

    def test_known_source_failures_persist_and_return_canonical_categories(self):
        cases = (
            ("network", httpx.ConnectError("provider-token=PRIVATE"), "NETWORK_ERROR", "NETWORK_ERROR"),
            ("rate", SourceError("SOURCE_RATE_LIMITED", retry_seconds=120), "RATE_LIMITED", "SOURCE_RATE_LIMITED"),
            ("changed", SourceError("SOURCE_CHANGED"), "SOURCE_CHANGED", "SOURCE_CHANGED"),
            ("blocked", SourceError("SOURCE_REDIRECT_BLOCKED"), "SOURCE_BLOCKED", "SOURCE_REDIRECT_BLOCKED"),
        )
        for suffix, provider_failure, category, health_error in cases:
            with self.subTest(category=category):
                source_id = self._source(suffix)
                with patch("services.discovery.sources.collect.source_adapter",
                           return_value=_FailingAdapter(provider_failure)):
                    with self.assertRaises(SourceError) as raised:
                        collect_source(self.store, source_id, "fixture")
                self.assertEqual(raised.exception.operation_error_category, category)
                self.assertNotIn("PRIVATE", str(raised.exception))
                source, observation, audit = self._persisted_failure(source_id)
                self.assertEqual(source["last_error"], health_error)
                self.assertEqual(observation["error_code"], health_error)
                self.assertEqual(audit["payload"], {
                    "error_category": category,
                    "provider": "greenhouse",
                })
                self.assertNotIn("PRIVATE", repr(audit))

    def test_safe_legacy_failure_is_rebuilt_and_preserves_only_its_code(self):
        private = "provider-token=PRIVATE-CAUSE"
        provider_failure = SourceError("SOURCE_INVALID_FORMAT")
        provider_failure.__cause__ = RuntimeError(private)
        provider_failure.__traceback__ = provider_failure.__cause__.__traceback__
        source_id = self._source("safe-legacy")

        with patch("services.discovery.sources.collect.source_adapter",
                   return_value=_FailingAdapter(provider_failure)):
            with self.assertRaises(SourceError) as raised:
                collect_source(self.store, source_id, "fixture")

        self.assertIsNot(raised.exception, provider_failure)
        self.assertEqual(raised.exception.code, "SOURCE_INVALID_FORMAT")
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertNotIn(private, repr(raised.exception))
        source, observation, audit = self._persisted_failure(source_id)
        self.assertEqual(source["last_error"], "SOURCE_INVALID_FORMAT")
        self.assertEqual(observation["error_code"], "SOURCE_INVALID_FORMAT")
        self.assertIsNone(audit)

    def test_unknown_and_secret_bearing_failures_are_sanitized_and_not_persisted(self):
        private = "provider-token=PRIVATE-SENTINEL"
        for suffix, provider_failure in (
            ("internal", RuntimeError(private)),
            ("unknown-source", SourceError(private)),
            ("identifier-secret", SourceError("PRIVATE_SENTINEL")),
            ("unclassified-http", SourceError("SOURCE_HTTP_404")),
            ("bare-status", SourceError(500)),
            ("unhashable-code", SourceError(["SOURCE_HTTP_503"])),
        ):
            with self.subTest(suffix=suffix):
                source_id = self._source(suffix)
                with patch("services.discovery.sources.collect.source_adapter",
                           return_value=_FailingAdapter(provider_failure)):
                    with self.assertRaises(SourceCollectionBoundaryError) as raised:
                        collect_source(self.store, source_id, "fixture")
                self.assertEqual(str(raised.exception), "SOURCE_COLLECTION_FAILED")
                self.assertNotIn(private, str(raised.exception))
                source, observation, audit = self._persisted_failure(source_id)
                self.assertIsNone(source["last_error"])
                self.assertIsNone(observation["error_code"])
                self.assertIsNone(audit)
                self.assertNotIn(private, repr(source))
                self.assertNotIn(private, repr(observation))

    def test_locked_source_change_returns_category_without_poisoning_provider_health(self):
        source_id = self._source("changed-after-fetch")

        def change_source(connection):
            connection.execute(update(m.job_sources).where(m.job_sources.c.id == source_id).values(
                tenant="changed-after-fetch"))

        with patch("services.discovery.sources.collect.source_adapter", return_value=_EmptyAdapter()):
            with self.assertRaises(SourceError) as raised:
                collect_source(self.store, source_id, "fixture", import_guard=change_source)

        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")
        self.assertEqual(raised.exception.operation_error_category, "SOURCE_CHANGED")
        with self.store.engine.connect() as connection:
            source = connection.execute(select(m.job_sources).where(
                m.job_sources.c.id == source_id)).mappings().one()
            observations = list(connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.configured_source_id == source_id)))
        self.assertEqual(source["health_status"], "NOT_CHECKED")
        self.assertIsNone(source["last_error"])
        self.assertEqual(observations, [])


if __name__ == "__main__":
    unittest.main()
