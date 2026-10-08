"""Connected API acceptance for safe company discovery activity readback."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


class CompanyDiscoveryActivityApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-company-activity-api-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'activity.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "activity-company-a", "name": "Acme Systems", "domain": "",
            })
            upsert(connection, m.companies, {
                "id": "activity-company-b", "name": "Northstar Labs", "domain": "",
            })
            connection.execute(m.audit.insert(), [
                {
                    "id": "activity-a-discovered", "entity_id": "fixture",
                    "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:00:00+00:00",
                    "payload": {"company_id": "activity-company-a",
                                "raw_payload": "SYNTHETIC_DISCOVERY_RAW_SECRET",
                                "source_url": "https://private-source.invalid/feed",
                                "imported_count": None, "matched_profile_count": None},
                },
                {
                    "id": "activity-a-enrolled", "entity_id": "fixture",
                    "action": "COMPANY_SOURCE_ENROLLED", "timestamp": "2026-10-08T09:05:00+00:00",
                    "payload": {"company_id": "activity-company-a",
                                "company_source_id": "synthetic-private-source-id",
                                "raw_payload": "SYNTHETIC_DISCOVERY_RAW_SECRET"},
                },
                {
                    "id": "activity-b-discovered", "entity_id": "fixture",
                    "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:10:00+00:00",
                    "payload": {"company_id": "activity-company-b",
                                "raw_payload": "SYNTHETIC_DISCOVERY_RAW_SECRET"},
                },
                {
                    "id": "activity-other-profile", "entity_id": "other",
                    "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:15:00+00:00",
                    "payload": {"company_id": "activity-company-b",
                                "raw_payload": "OTHER_PROFILE_PRIVATE_SECRET"},
                },
                {
                    "id": "activity-unknown", "entity_id": "fixture",
                    "action": "COMPANY_DISCOVERY_DIAGNOSTIC", "timestamp": "2026-10-08T09:20:00+00:00",
                    "payload": {"company_id": "activity-company-b",
                                "raw_payload": "SYNTHETIC_UNKNOWN_EVENT_SECRET",
                                "imported_count": 777},
                },
            ])
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            operation_mode="PERSONAL"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_profile_activity_is_newest_first_bounded_and_read_back_from_storage(self):
        response = self.client.get("/profiles/fixture/company-discovery/activity?limit=2")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        events = body["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [(event["event_type"], event["summary"], event["timestamp"])
             for event in events],
            [
                ("COMPANY_DISCOVERED", "Discovered Northstar Labs", "2026-10-08T09:10:00+00:00"),
                ("COMPANY_SOURCE_ENROLLED", "Added direct employer source for Acme Systems", "2026-10-08T09:05:00+00:00"),
            ],
        )

        # A second request reads the same durable timeline; no client-side fixture
        # state is needed to reconstruct the result.
        readback = self.client.get("/profiles/fixture/company-discovery/activity?limit=10")
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(len(readback.json()["events"]), 3)
        self.assertEqual(readback.json()["events"][0], events[0])

    def test_activity_is_profile_isolated_and_excludes_raw_and_unknown_data(self):
        response = self.client.get("/profiles/fixture/company-discovery/activity")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        events = body["events"]
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(len(events), 3)
        self.assertEqual(
            [(event["event_type"], event["summary"]) for event in events],
            [
                ("COMPANY_DISCOVERED", "Discovered Northstar Labs"),
                ("COMPANY_SOURCE_ENROLLED", "Added direct employer source for Acme Systems"),
                ("COMPANY_DISCOVERED", "Discovered Acme Systems"),
            ],
        )

        other = self.client.get("/profiles/other/company-discovery/activity")
        self.assertEqual(other.status_code, 200, other.text)
        other_events = other.json()["events"]
        self.assertEqual([(event["event_type"], event["summary"]) for event in other_events], [
            ("COMPANY_DISCOVERED", "Discovered Northstar Labs"),
        ])

        serialized = response.text
        for private_value in (
            "SYNTHETIC_DISCOVERY_RAW_SECRET",
            "https://private-source.invalid/feed",
            "synthetic-private-source-id",
            "OTHER_PROFILE_PRIVATE_SECRET",
            "SYNTHETIC_UNKNOWN_EVENT_SECRET",
            "COMPANY_DISCOVERY_DIAGNOSTIC",
            "imported_count",
            "matched_profile_count",
        ):
            self.assertNotIn(private_value, serialized)
        self.assertTrue(all(set(event) == {"timestamp", "event_type", "summary"}
                            for event in events))
        self.assertTrue(all(event["event_type"] != "COMPANY_DISCOVERY_DIAGNOSTIC"
                            for event in events))


if __name__ == "__main__":
    unittest.main()
