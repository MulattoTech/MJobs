"""Supported provider collection passively records third-party source candidates."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import configure_source
from services.discovery.sources.contract import SearchBatch, SourceCompany, SourceItem
from services.discovery.sources.remoteok import RemoteOK
from services.source_candidates.catalogue import list_candidates
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation
from workers.tasks import execute_operation


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

    def test_supported_collection_records_profile_scoped_candidate_without_extra_provider_call(self):
        adapter = RemoteOK(transport=None)
        operation_id = enqueue_operation(self.store, "DISCOVER", "fixture", {
            "source_id": self.source_id,
            "profile_ids": ["fixture", "other"],
        })
        environment = {
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:8090",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
        }
        with patch.object(adapter, "search", return_value=self.batch) as search:
            with patch("services.discovery.sources.collect.source_adapter", return_value=adapter), \
                    patch.dict(os.environ, environment):
                result = execute_operation(operation_id)
                self.assertEqual(result["status"], "COMPLETED", result)
                replay = execute_operation(operation_id)
            self.assertEqual(replay["status"], "ALREADY_CLAIMED_OR_CAPACITY")
            search.assert_called_once_with()

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
