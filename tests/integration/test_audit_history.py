"""The activity timeline exposes safe profile-scoped append-only history."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.discovery.matching import recalculate
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class AuditHistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-audit-history-")
        self.root = Path(self.directory.name)
        self.database = self.root / "trial.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id="private-audit-fixture", entity_id="fixture", action="SETTING_CHANGED",
                payload={"secret": "never expose this payload"},
                timestamp="2026-10-07T10:00:00+00:00"))
            connection.execute(m.audit.insert().values(
                id="other-profile-audit", entity_id="other", action="SETTING_CHANGED",
                payload={"secret": "other profile private payload"},
                timestamp="2026-10-07T10:01:00+00:00"))
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_timeline_is_safe_scoped_and_persistent_across_store_reopen(self):
        self.assertEqual(self.store.application(self.application_id)["profile_id"], "fixture")
        response = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertTrue(any(event["type"] == "JOB_DISCOVERED" for event in body["events"]), body)
        self.assertTrue(any(event["type"] == "MATCH_SCORED" for event in body["events"]), body)
        self.assertTrue(any(event["type"] == "SETTING_CHANGED" for event in body["events"]))
        self.assertTrue(any(event["type"] == "RESUME_GENERATED" for event in body["events"]), body)
        self.assertTrue(any(event["company"] for event in body["events"]))
        serialized = response.text
        self.assertNotIn("never expose this payload", serialized)
        self.assertNotIn("other profile private payload", serialized)
        self.assertNotIn("other-profile-audit", serialized)
        other_profile = self.client.get("/profiles/other/audit-history")
        self.assertEqual(other_profile.status_code, 200, other_profile.text)
        self.assertFalse(any(event["type"] in {"JOB_DISCOVERED", "MATCH_SCORED"}
                             for event in other_profile.json()["events"]))

        with self.store.engine.connect() as connection:
            count_before = connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == self.application_id)).all()
        self.assertTrue(count_before)
        self.store.engine.dispose()
        reopened = Store(f"sqlite:///{self.database.as_posix()}")
        reopened_client = TestClient(create_app(reopened, "http://127.0.0.1:1", self.root,
                                                operation_mode="FIXTURE"))
        try:
            after_restart = reopened_client.get("/profiles/fixture/audit-history")
            self.assertEqual(after_restart.status_code, 200, after_restart.text)
            self.assertEqual(after_restart.json(), body)
        finally:
            reopened_client.close()
            reopened.engine.dispose()

    def test_unknown_profile_does_not_receive_an_empty_success_timeline(self):
        response = self.client.get("/profiles/not-a-profile/audit-history")
        self.assertEqual(response.status_code, 404)

    def test_user_reported_application_history_appears_without_exposing_note_or_claiming_receipt(self):
        from services.tracking.applications import (
            add_application_note, record_external_application_report,
            set_application_pipeline_status,
        )

        set_application_pipeline_status(self.store, "fixture", self.application_id,
                                        "APPLIED", "audit-status-applied")
        add_application_note(self.store, "fixture", self.application_id,
                             "Private recruiter follow-up detail", "audit-note-private")
        record_external_application_report(
            self.store, "fixture", self.application_id, "audit-external-status",
            "audit-external-capture", "a" * 64, "MANUAL", "2026-10-07T12:00:00Z",
            None, self.store.application(self.application_id)["job_id"], "MATCHED")

        response = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        tracked = [event for event in body["events"]
                   if event["type"] in {
                       "APPLICATION_STATUS_REPORTED", "APPLICATION_NOTE_ADDED", "APPLICATION_REPORTED"
                   }]
        self.assertEqual({event["type"] for event in tracked},
                         {"APPLICATION_STATUS_REPORTED", "APPLICATION_NOTE_ADDED", "APPLICATION_REPORTED"})
        self.assertTrue(all(event["subject"] for event in tracked))
        self.assertNotIn("Private recruiter follow-up detail", response.text)
        self.assertNotIn("SUBMISSION_CONFIRMED", response.text)
        other_profile = self.client.get("/profiles/other/audit-history")
        self.assertEqual(other_profile.status_code, 200, other_profile.text)
        self.assertFalse(any(event["type"] in {
            "APPLICATION_STATUS_REPORTED", "APPLICATION_NOTE_ADDED", "APPLICATION_REPORTED"
        } for event in other_profile.json()["events"]))

    def test_discovery_events_survive_scoring_recalculation_without_mutation(self):
        initial = self.client.get("/profiles/fixture/audit-history").json()["events"]
        initial_job_events = [event for event in initial
                              if event["type"] in {"JOB_DISCOVERED", "MATCH_SCORED"}]
        self.assertTrue(initial_job_events)
        recalculate(self.store, "fixture")
        repeated = self.client.get("/profiles/fixture/audit-history").json()["events"]
        repeated_job_events = [event for event in repeated
                               if event["type"] in {"JOB_DISCOVERED", "MATCH_SCORED"}]
        self.assertEqual(repeated_job_events, initial_job_events)

    def test_recalculation_records_first_profile_discovery_then_stays_idempotent(self):
        recalculate(self.store, "other")
        first = self.client.get("/profiles/other/audit-history").json()["events"]
        first_job_events = [event for event in first
                            if event["type"] in {"JOB_DISCOVERED", "MATCH_SCORED"}]
        self.assertTrue(first_job_events)
        recalculate(self.store, "other")
        second = self.client.get("/profiles/other/audit-history").json()["events"]
        second_job_events = [event for event in second
                             if event["type"] in {"JOB_DISCOVERED", "MATCH_SCORED"}]
        self.assertEqual(second_job_events, first_job_events)
        self.assertTrue(all(event["company"] for event in second_job_events))
