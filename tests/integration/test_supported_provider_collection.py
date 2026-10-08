"""Supported provider collection passively records third-party source candidates."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.discovery.sources.contract import SearchBatch, SourceCompany, SourceItem
from services.discovery.sources.remoteok import RemoteOK
from services.source_candidates.catalogue import list_candidates
from tests.integration.test_spine import PROFILE


class SupportedProviderCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-supported-provider-capture-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'collection.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        self.source_id = configure_source(self.store, "fixture", provider="remoteok")
        self.endpoint = "https://remoteok.com/api"
        self.company = SourceCompany(
            identity_parts=("remoteok", "example-company"),
            name="Example Company",
            domain="https://remoteok.com",
        )
        self.batch = SearchBatch(endpoint=self.endpoint, items=(
            SourceItem({
                "id": 51701,
                "position": "Systems Administrator",
                "company": "Example Company",
                "location": "Remote",
                "description": "Maintain Linux systems and automate operations.",
                "url": "https://careers.example.invalid/jobs/51701",
            }, self.company),
            SourceItem({
                "id": 51702,
                "position": "Platform Administrator",
                "company": "Example Company",
                "location": "Remote",
                "description": "Operate cloud infrastructure and Linux services.",
                "url": "https://remoteok.com/remote-jobs/51702",
            }, self.company),
        ))

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _collect_with_stubbed_provider(self, profile_id, adapter):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.job_sources).where(
                m.job_sources.c.id == self.source_id
            ).values(last_attempt=None))
        with patch("services.discovery.sources.collect.source_adapter", return_value=adapter):
            return collect_source(self.store, self.source_id, profile_id)

    def test_supported_collection_records_profile_scoped_candidate_without_extra_provider_call(self):
        adapter = RemoteOK(transport=None)
        with patch.object(adapter, "search", return_value=self.batch) as search:
            result = self._collect_with_stubbed_provider("fixture", adapter)
            self.assertEqual(result["jobs_seen"], 2)
            search.assert_called_once_with()

            # Replaying the same collection is idempotent at the catalogue boundary.
            self._collect_with_stubbed_provider("fixture", adapter)
            self.assertEqual(search.call_count, 2)

            # The same supported collection under another profile remains isolated.
            self._collect_with_stubbed_provider("other", adapter)
            self.assertEqual(search.call_count, 3)

        fixture_candidates = list_candidates(self.store, "fixture")["candidates"]
        other_candidates = list_candidates(self.store, "other")["candidates"]
        self.assertEqual([candidate["domain"] for candidate in fixture_candidates],
                         ["careers.example.invalid"])
        self.assertEqual([candidate["domain"] for candidate in other_candidates],
                         ["careers.example.invalid"])

        candidate = fixture_candidates[0]
        self.assertFalse(candidate["collection_enabled"])
        self.assertEqual(candidate["state"], "NEEDS_REVIEW")
        self.assertEqual(len(candidate["observations"]), 1)
        observation = candidate["observations"][0]
        self.assertEqual(observation["capture_method"], "SUPPORTED_COLLECTION")
        self.assertEqual(observation["evidence_status"], "PARTIAL")
        self.assertEqual(observation["source_url"], self.endpoint)
        self.assertEqual(observation["application_url"],
                         "https://careers.example.invalid/jobs/51701")
        self.assertEqual(observation["observed_hops"], [{"state": "UNKNOWN"}])
        self.assertEqual(len(other_candidates[0]["observations"]), 1)

        with self.store.engine.connect() as connection:
            native_domain_candidate = connection.execute(select(m.source_candidates.c.id).where(
                m.source_candidates.c.profile_id == "fixture",
                m.source_candidates.c.domain == "remoteok.com",
            )).first()
        self.assertIsNone(native_domain_candidate)


if __name__ == "__main__":
    unittest.main()
