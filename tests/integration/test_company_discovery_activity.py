"""Company discovery activity is chronological, profile-scoped, and value-safe."""
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.activity import list_company_discovery_activity
from tests.integration.test_spine import PROFILE


class CompanyDiscoveryActivityApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-company-activity-")
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
            # Include deliberately sensitive and incomplete discovery metadata in the
            # append-only source record. The public timeline must derive a safe summary.
            connection.execute(m.audit.insert(), [
                {
                    "id": "activity-company-a-discovered", "entity_id": "fixture",
                    "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:00:00+00:00",
                    "payload": {"company_id": "activity-company-a",
                                "raw_payload": "SYNTHETIC_RAW_DISCOVERY_SECRET",
                                "imported_count": None, "matched_profile_count": None},
                },
                {
                    "id": "activity-company-a-enrolled", "entity_id": "fixture",
                    "action": "COMPANY_SOURCE_ENROLLED", "timestamp": "2026-10-08T09:05:00+00:00",
                    "payload": {"company_id": "activity-company-a",
                                "company_source_id": "synthetic-source-private-id",
                                "raw_payload": "SYNTHETIC_RAW_DISCOVERY_SECRET",
                                "imported_count": None},
                },
                {
                    "id": "activity-company-b-discovered", "entity_id": "fixture",
                    "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:10:00+00:00",
                    "payload": {"company_id": "activity-company-b",
                                "raw_payload": "SYNTHETIC_RAW_DISCOVERY_SECRET",
                                "imported_count": None},
                },
                {
                    "id": "activity-other-profile-company", "entity_id": "other",
                    "action": "COMPANY_DISCOVERED", "timestamp": "2026-10-08T09:15:00+00:00",
                    "payload": {"company_id": "activity-company-b",
                                "raw_payload": "OTHER_PROFILE_DISCOVERY_SECRET"},
                },
                {
                    "id": "activity-unknown-event", "entity_id": "fixture",
                    "action": "COMPANY_DISCOVERY_DIAGNOSTIC", "timestamp": "2026-10-08T09:20:00+00:00",
                    "payload": {"company_id": "activity-company-b", "imported_count": None,
                                "raw_payload": "SYNTHETIC_UNKNOWN_EVENT_SECRET"},
                },
            ])
    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_known_company_events_are_newest_first_profile_scoped_and_sanitized(self):
        events = list_company_discovery_activity(self.store, "fixture")
        self.assertEqual(
            [(event["event_type"], event["summary"], event["timestamp"])
             for event in events],
            [
                ("COMPANY_DISCOVERED", "Discovered Northstar Labs", "2026-10-08T09:10:00+00:00"),
                ("COMPANY_SOURCE_ENROLLED", "Added direct employer source for Acme Systems", "2026-10-08T09:05:00+00:00"),
                ("COMPANY_DISCOVERED", "Discovered Acme Systems", "2026-10-08T09:00:00+00:00"),
            ],
        )
        self.assertTrue(all(set(event) == {"timestamp", "summary", "event_type"}
                            for event in events))
        serialized = repr(events)
        self.assertNotIn("SYNTHETIC_RAW_DISCOVERY_SECRET", serialized)
        self.assertNotIn("synthetic-source-private-id", serialized)
        self.assertNotIn("OTHER_PROFILE_DISCOVERY_SECRET", serialized)
        self.assertNotIn("SYNTHETIC_UNKNOWN_EVENT_SECRET", serialized)
        self.assertNotIn("imported_count", serialized)
        self.assertNotIn("matched_profile_count", serialized)
        self.assertNotIn("unknown", serialized.lower())

        other_events = list_company_discovery_activity(self.store, "other")
        self.assertEqual([(event["event_type"], event["summary"])
                          for event in other_events], [
            ("COMPANY_DISCOVERED", "Discovered Northstar Labs"),
        ])


if __name__ == "__main__":
    unittest.main()
