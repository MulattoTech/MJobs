"""Connected API acceptance for safe company discovery activity readback."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.domains import record_company_domain
from services.discovery.sources.collect import collect_source, configure_source
from tests.integration.test_greenhouse import RAW
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

    def test_activity_explains_profile_discovery_trigger_attempt_evidence_and_safe_error(self):
        source = configure_source(self.store, "activity-discovery")

        def response(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": [RAW]})
            return httpx.Response(200, json={"name": "Example Public Employer"})

        collect_source(
            self.store, source, "fixture", transport=httpx.MockTransport(response),
        )
        with self.store.engine.begin() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.profile_id == "fixture"
            )).mappings().one())
            connection.execute(m.operations.insert().values(
                id="company-history-attempt", kind="COMPANY_DISCOVER",
                entity_id=request["company_id"],
                params={"profile_id": "fixture", "private_url": "https://private.invalid/?token=x"},
                state="DEAD_LETTERED", error="COMPANY_OFFICIAL_WEBSITE_NOT_FOUND",
                updated_at="2026-10-08T10:10:00+00:00", attempt_count=2,
                last_attempt_at="2026-10-08T10:09:00+00:00",
                dead_lettered_at="2026-10-08T10:10:00+00:00",
            ))
            connection.execute(update(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request["id"]
            ).values(operation_id="company-history-attempt", authorization="USER_REVIEWED"))
            connection.execute(m.audit.insert().values(
                id="company-history-ats-match", entity_id=request["company_id"],
                action="COMPANY_ATS_DETECTION_REVIEW",
                timestamp="2026-10-08T10:08:00+00:00",
                payload={"status": "MATCH", "provider": "greenhouse",
                         "confidence": 0.94, "evidence": ["PUBLIC_API_BOARD"],
                         "private_url": "https://private.invalid/ats"},
            ))
            connection.execute(m.audit.insert().values(
                id="company-history-profile-discovered", entity_id="fixture",
                action="COMPANY_DISCOVERED", timestamp="2026-10-08T10:07:00+00:00",
                payload={"company_id": request["company_id"], "raw_payload": "PRIVATE"},
            ))
        record_company_domain(
            self.store, request["company_id"], "employer.example",
            "VERIFIED_EXISTING_DOMAIN", ["Synthetic first-party evidence"],
        )

        response = self.client.get("/profiles/fixture/company-discovery/activity")

        self.assertEqual(response.status_code, 200, response.text)
        attempt = next(event for event in response.json()["events"]
                       if event["event_type"] == "COMPANY_DISCOVERY_ATTEMPT")
        self.assertEqual(attempt["summary"], "Company discovery attempt needs review.")
        self.assertEqual(attempt["details"]["company"], "Example Public Employer")
        self.assertIn("Previously unseen company", attempt["details"]["triggers"])
        self.assertIn("High-match job", attempt["details"]["triggers"])
        self.assertEqual(attempt["details"]["attempt_state"], "DEAD_LETTERED")
        self.assertEqual(attempt["details"]["attempt_count"], 2)
        self.assertEqual(attempt["details"]["ats_provider"], "Greenhouse")
        self.assertEqual(attempt["details"]["confidence"], 0.94)
        self.assertEqual(attempt["details"]["confidence_basis"], "ATS detection")
        self.assertIn("Official domain verified", attempt["details"]["evidence"])
        self.assertFalse(attempt["details"]["source_created"])
        self.assertEqual(
            attempt["details"]["error"],
            "No official employer website could be verified.",
        )
        self.assertNotIn("private.invalid", response.text)
        self.assertNotIn("PUBLIC_API_BOARD", response.text)

        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == "company-history-attempt"
            ).values(error="https://private.invalid/raw?token=secret"))
        unknown_error = self.client.get("/profiles/fixture/company-discovery/activity")
        safe_attempt = next(event for event in unknown_error.json()["events"]
                            if event["event_type"] == "COMPANY_DISCOVERY_ATTEMPT")
        self.assertEqual(
            safe_attempt["details"]["error"],
            "The discovery attempt ended with an error that needs review.",
        )
        self.assertNotIn("private.invalid", unknown_error.text)
        self.assertNotIn("secret", unknown_error.text)

        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == "company-history-attempt"
            ).values(state="COMPLETED", error=None,
                     updated_at="2026-10-08T10:11:00+00:00"))
            connection.execute(m.audit.insert(), [
                {
                    "id": "company-history-source-created", "entity_id": request["company_id"],
                    "action": "COMPANY_SOURCE_DISCOVERED", "timestamp": "2026-10-08T10:10:00+00:00",
                    "payload": {"jobs_enumerated": 5, "jobs_imported": 3,
                                "endpoint": "https://private.invalid/feed"},
                },
                {
                    "id": "company-history-feed-result", "entity_id": request["company_id"],
                    "action": "COMPANY_FEED_IMPORT_COMPLETE", "timestamp": "2026-10-08T10:10:30+00:00",
                    "payload": {"profile_id": "fixture", "jobs_enumerated": 5,
                                "jobs_imported": 3, "matched_jobs": 2,
                                "raw_payload": "PRIVATE"},
                },
            ])
        completed = self.client.get("/profiles/fixture/company-discovery/activity")
        completed_attempt = next(event for event in completed.json()["events"]
                                 if event["event_type"] == "COMPANY_DISCOVERY_ATTEMPT")
        self.assertTrue(completed_attempt["details"]["source_created"])
        self.assertEqual(completed_attempt["details"]["results"], {
            "jobs_enumerated": 5, "jobs_imported": 3, "matched_jobs": 2,
        })
        self.assertNotIn("private.invalid", completed.text)
        self.assertNotIn("PRIVATE", completed.text)


if __name__ == "__main__":
    unittest.main()
