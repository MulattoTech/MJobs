"""Profile-scoped, privacy-safe company discovery queue status projection."""
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.queue_status import read_company_discovery_queue_status
from tests.integration.test_spine import PROFILE


class CompanyDiscoveryQueueStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'queue.db').as_posix()}")
        self.store.migrate()
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_projects_stable_states_safe_counts_and_profile_audit_aggregates(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert(), [
                {"id": "queued-private-id", "kind": "COMPANY_DISCOVER", "entity_id": "company-a",
                 "params": {"profile_id": "fixture", "dispatch_priority": "HIGH",
                            "candidate": {"company_name": "Sensitive Provider Content"}},
                 "state": "QUEUED", "updated_at": "2026-10-10T10:00:00Z", "attempt_count": 0},
                {"id": "running-private-id", "kind": "COMPANY_DISCOVER", "entity_id": "company-b",
                 "params": {"profile_id": "fixture", "private_url": "https://secret.example/?token=abc"},
                 "state": "RUNNING", "error": "PRIVATE_PROVIDER_ERROR", "updated_at": "2026-10-10T10:01:00Z",
                 "attempt_count": 2},
                {"id": "failed-private-id", "kind": "COMPANY_DISCOVER", "entity_id": "company-c",
                 "params": {"profile_id": "fixture"}, "state": "DEAD_LETTERED",
                 "error": "PRIVATE_PROVIDER_ERROR", "updated_at": "2026-10-10T10:02:00Z",
                 "attempt_count": 3},
                {"id": "other-profile-operation", "kind": "COMPANY_DISCOVER", "entity_id": "company-d",
                 "params": {"profile_id": "other"}, "state": "QUEUED",
                 "updated_at": "2026-10-10T10:03:00Z", "attempt_count": 0},
                {"id": "unscoped-operation", "kind": "COMPANY_DISCOVER", "entity_id": "company-e",
                 "params": {"candidate": {"company_name": "unscoped"}}, "state": "QUEUED",
                 "updated_at": "2026-10-10T10:04:00Z", "attempt_count": 0},
            ])
            connection.execute(m.audit.insert(), [
                {"id": "profile-discovered", "entity_id": "fixture", "action": "COMPANY_DISCOVERED",
                 "timestamp": "2026-10-10T09:00:00Z", "payload": {"company_id": "company-a", "raw": "SECRET"}},
                {"id": "profile-enrolled", "entity_id": "fixture", "action": "COMPANY_SOURCE_ENROLLED",
                 "timestamp": "2026-10-10T09:01:00Z", "payload": {"company_id": "company-a", "raw": "SECRET"}},
                {"id": "other-profile-discovered", "entity_id": "other", "action": "COMPANY_DISCOVERED",
                 "timestamp": "2026-10-10T09:02:00Z", "payload": {"raw": "OTHER_SECRET"}},
            ])

        result = read_company_discovery_queue_status(self.store, "fixture")

        self.assertEqual(result["counts"], {
            "total": 3, "queued": 1, "running": 1, "succeeded": 0,
            "needs_review": 1, "cancelled": 0, "unknown": 0,
        })
        self.assertEqual(result["activity_counts"], {"companies_discovered": 1, "sources_enrolled": 1})
        self.assertEqual([item["state"] for item in result["items"]], ["NEEDS_REVIEW", "RUNNING", "QUEUED"])
        self.assertEqual(result["items"][-1]["priority"], "HIGH")
        rendered = repr(result)
        for private_value in ("queued-private-id", "running-private-id", "PRIVATE_PROVIDER_ERROR",
                              "secret.example", "Sensitive Provider Content", "OTHER_SECRET"):
            self.assertNotIn(private_value, rendered)

    def test_normalizes_terminal_states_and_bounds_results(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert(), [
                {"id": f"operation-{index}", "kind": "COMPANY_DISCOVER", "entity_id": f"company-{index}",
                 "params": {"profile_id": "fixture"}, "state": state,
                 "updated_at": f"2026-10-10T10:{index:02d}:00Z", "attempt_count": index}
                for index, state in enumerate(("SUCCEEDED", "COMPLETED", "CANCELLED", "DISCARDED", "UNKNOWN_STATE"))
            ])

        result = read_company_discovery_queue_status(self.store, "fixture", limit=2)

        self.assertEqual(result["counts"], {
            "total": 5, "queued": 0, "running": 0, "succeeded": 2,
            "needs_review": 0, "cancelled": 2, "unknown": 1,
        })
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual({item["state"] for item in result["items"]}, {"UNKNOWN", "CANCELLED"})

    def test_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            read_company_discovery_queue_status(self.store, "fixture", limit=True)

    def test_empty_queue_returns_zeroed_counts(self):
        result = read_company_discovery_queue_status(self.store, "fixture")

        self.assertEqual(result["counts"], {
            "total": 0, "queued": 0, "running": 0, "succeeded": 0,
            "needs_review": 0, "cancelled": 0, "unknown": 0,
        })
        self.assertEqual(result["activity_counts"], {"companies_discovered": 0, "sources_enrolled": 0})
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
