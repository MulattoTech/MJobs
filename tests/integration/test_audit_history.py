"""The activity timeline exposes safe profile-scoped append-only history."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.audit_history import record_company_source_enrolled
from services.company_discovery.sources import (
    set_company_source_enabled,
    set_company_source_pin,
    upsert_company_job_source,
)
from services.discovery.company_feed import import_company_feed_jobs
from services.discovery.pipeline import discover
from services.discovery.matching import recalculate
from services.documents.generate import prepare
from services.discovery.search_profiles import save_search_profile
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

    def test_profile_preference_change_appends_value_free_setting_event_once(self):
        before = self.client.get("/profiles/fixture/audit-history").json()["events"]
        before_ids = {event["id"] for event in before if event["type"] == "SETTING_CHANGED"}
        current = self.store.profile("fixture")
        private_value = "Synthetic confidential salary preference"
        payload = dict(PROFILE, answers=current["answers"],
                       preferences={**current["preferences"],
                                    "salary_expectations": private_value},
                       expected_version=current["version"])

        saved = self.client.put("/profiles", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        first = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(first.status_code, 200, first.text)
        events = first.json()["events"]
        added = [event for event in events if event["type"] == "SETTING_CHANGED"
                 and event["id"] not in before_ids]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["summary"], "A profile setting changed")
        self.assertNotIn(private_value, first.text)
        self.assertNotIn(private_value, str(added[0]))
        self.assertFalse(any(event["id"] == added[0]["id"]
                             for event in self.client.get(
                                 "/profiles/other/audit-history").json()["events"]))
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.id == added[0]["id"])).scalar_one()
        self.assertEqual(row, {})

        payload["expected_version"] = saved.json()["version"]
        repeated = self.client.put("/profiles", json=payload)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        after = self.client.get("/profiles/fixture/audit-history").json()["events"]
        after_added = [event for event in after if event["type"] == "SETTING_CHANGED"
                       and event["id"] not in before_ids]
        self.assertEqual(after_added, added)

    def test_changed_search_profile_appends_generic_setting_event_once(self):
        search_profile = {
            "id": "audit-search-profile",
            "profile_id": "fixture",
            "name": "Synthetic saved search",
            "include_titles": ["Private synthetic search phrase"],
            "source_ids": [],
        }
        before = self.client.get("/profiles/fixture/audit-history").json()["events"]
        before_setting_ids = {event["id"] for event in before
                              if event["type"] == "SETTING_CHANGED"}

        save_search_profile(self.store, search_profile)
        created = self.client.get("/profiles/fixture/audit-history").json()["events"]
        self.assertEqual({event["id"] for event in created
                          if event["type"] == "SETTING_CHANGED"}, before_setting_ids)

        save_search_profile(self.store, {**search_profile, "enabled": False})
        changed = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(changed.status_code, 200, changed.text)
        changed_events = [event for event in changed.json()["events"]
                          if event["type"] == "SETTING_CHANGED"
                          and event["id"] not in before_setting_ids]
        self.assertEqual(len(changed_events), 1)
        self.assertEqual(changed_events[0]["summary"], "A profile setting changed")
        self.assertNotIn("Private synthetic search phrase", changed.text)
        with self.store.engine.connect() as connection:
            payload = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.id == changed_events[0]["id"])).scalar_one()
        self.assertEqual(payload, {})

        save_search_profile(self.store, {**search_profile, "enabled": False})
        repeated = self.client.get("/profiles/fixture/audit-history").json()["events"]
        self.assertEqual(len([event for event in repeated
                              if event["type"] == "SETTING_CHANGED"]),
                         len(before_setting_ids) + 1)
        other_profile = self.client.get("/profiles/other/audit-history").json()["events"]
        self.assertFalse(any(event["id"] == changed_events[0]["id"]
                             for event in other_profile))

    def test_company_source_changes_append_setting_events_to_enrolled_profiles_only(self):
        company_id = "audit-source-settings-company"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": "Source Settings Employer", "domain": "",
            })
        source = upsert_company_job_source(
            self.store, company_id=company_id, provider="rss", tenant_identifier="settings-board",
            public_api_url="https://source-settings.example/jobs.json",
            collection_strategy="PUBLIC_JSON", profile_id="fixture",
        )
        with self.store.engine.begin() as connection:
            record_company_source_enrolled(connection, "other", company_id, source["id"])

        initial = {
            profile_id: self.client.get(f"/profiles/{profile_id}/audit-history").json()["events"]
            for profile_id in ("fixture", "other")
        }
        initial_ids = {
            profile_id: {event["id"] for event in events if event["type"] == "SETTING_CHANGED"}
            for profile_id, events in initial.items()
        }

        set_company_source_pin(self.store, source["id"], pinned=True)
        set_company_source_pin(self.store, source["id"], pinned=True)
        set_company_source_enabled(self.store, source["id"], enabled=False)
        set_company_source_enabled(self.store, source["id"], enabled=False)

        for profile_id in ("fixture", "other"):
            history = self.client.get(f"/profiles/{profile_id}/audit-history")
            self.assertEqual(history.status_code, 200, history.text)
            setting_events = [event for event in history.json()["events"]
                              if event["type"] == "SETTING_CHANGED"]
            added = [event for event in setting_events if event["id"] not in initial_ids[profile_id]]
            self.assertEqual(len(added), 2, history.text)
            self.assertTrue(all(event["summary"] == "A profile setting changed" for event in added))
            self.assertNotIn(source["id"], history.text)
            self.assertNotIn("USER_PINNED", history.text)
            self.assertNotIn("COMPANY_SOURCE_ENABLED_CHANGED", history.text)

        with self.store.engine.connect() as connection:
            profile_rows = connection.execute(select(m.audit.c.entity_id, m.audit.c.payload).where(
                m.audit.c.action == "SETTING_CHANGED",
                m.audit.c.entity_id.in_(("fixture", "other")),
            )).all()
            source_rows = connection.execute(select(m.audit.c.action).where(
                m.audit.c.entity_id == source["id"],
                m.audit.c.action.in_(("COMPANY_SOURCE_PIN_CHANGED", "COMPANY_SOURCE_ENABLED_CHANGED")),
            )).all()
        value_free_profile_rows = [row for row in profile_rows if row.payload == {}]
        self.assertEqual(len(value_free_profile_rows), 4)
        self.assertEqual({row.action for row in source_rows}, {
            "COMPANY_SOURCE_PIN_CHANGED", "COMPANY_SOURCE_ENABLED_CHANGED",
        })

        unlinked = upsert_company_job_source(
            self.store, company_id=company_id, provider="greenhouse", tenant_identifier="unlinked-board",
            public_api_url="https://source-settings.example/greenhouse.json",
            collection_strategy="PUBLIC_JSON",
        )
        before_unlinked = {
            profile_id: {event["id"] for event in self.client.get(
                f"/profiles/{profile_id}/audit-history").json()["events"]
                         if event["type"] == "SETTING_CHANGED"}
            for profile_id in ("fixture", "other")
        }
        set_company_source_pin(self.store, unlinked["id"], pinned=True)
        for profile_id in ("fixture", "other"):
            after = self.client.get(f"/profiles/{profile_id}/audit-history").json()["events"]
            self.assertEqual({event["id"] for event in after
                              if event["type"] == "SETTING_CHANGED"}, before_unlinked[profile_id])

    def test_new_feed_posting_attached_to_existing_job_records_profile_merge_once(self):
        company_id = "audit-merge-company"
        application_url = "https://employer.example/careers/apply/req-1001"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company_id, "name": "Merge Employer", "domain": "employer.example",
            })
        first_source = upsert_company_job_source(
            self.store, company_id=company_id, provider="rss", tenant_identifier="rss-board",
            public_api_url="https://employer.example/rss.json", collection_strategy="PUBLIC_JSON",
        )
        second_source = upsert_company_job_source(
            self.store, company_id=company_id, provider="greenhouse", tenant_identifier="gh-board",
            public_api_url="https://employer.example/greenhouse.json", collection_strategy="PUBLIC_JSON",
        )

        def item(external_id, title):
            return {"id": external_id, "title": title, "description": "Systems administration",
                    "location": "Remote", "url": application_url}

        import_company_feed_jobs(
            self.store, company_id=company_id, source_id=first_source["id"], provider="rss",
            tenant="rss-board", endpoint=first_source["endpoint"], strategy="PUBLIC_JSON",
            items=[item("rss-req-1001", "Systems Administrator")], profile_id="fixture",
        )
        with self.store.engine.connect() as connection:
            first_job_id = connection.execute(m.routes.select().where(
                m.routes.c.company_id == company_id)).mappings().one()["job_id"]

        def reject_commit(_connection):
            raise RuntimeError("synthetic import receipt failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic import receipt failure"):
            import_company_feed_jobs(
                self.store, company_id=company_id, source_id=second_source["id"], provider="greenhouse",
                tenant="gh-board", endpoint=second_source["endpoint"], strategy="PUBLIC_JSON",
                items=[item("gh-req-1001", "Systems Administrator")], profile_id="fixture",
                import_receipt=reject_commit,
            )
        self.assertFalse(any(event["type"] == "JOB_MERGED" for event in
                             self.client.get("/profiles/fixture/audit-history").json()["events"]))
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(m.postings.select().where(
                m.postings.c.source == f"company-feed:{second_source['id']}")).all(), [])

        import_company_feed_jobs(
            self.store, company_id=company_id, source_id=second_source["id"], provider="greenhouse",
            tenant="gh-board", endpoint=second_source["endpoint"], strategy="PUBLIC_JSON",
            items=[item("gh-req-1001", "Systems Administrator")], profile_id="fixture",
        )
        response = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["events"]
        merged = [event for event in events if event["type"] == "JOB_MERGED"]
        self.assertEqual(len(merged), 1, events)
        self.assertEqual(merged[0]["summary"], "New source posting attached to an existing job")
        self.assertEqual(merged[0]["subject"], "Systems Administrator")
        self.assertEqual(merged[0]["company"], "Merge Employer")
        self.assertNotIn(application_url, response.text)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.id == merged[0]["id"])).scalar_one(), {"job_id": first_job_id})
        self.assertFalse(any(event["type"] == "JOB_MERGED" for event in
                             self.client.get("/profiles/other/audit-history").json()["events"]))
        with self.store.engine.connect() as connection:
            postings = connection.execute(m.postings.select().where(
                m.postings.c.job_id == first_job_id)).mappings().all()
            job_ids = {posting["job_id"] for posting in postings
                       if posting["source"] in {f"company-feed:{first_source['id']}",
                                                 f"company-feed:{second_source['id']}"}}
        self.assertEqual(len(postings), 2)
        self.assertEqual(job_ids, {first_job_id})

        # Re-reading the same provider posting refreshes it without inventing another merge.
        import_company_feed_jobs(
            self.store, company_id=company_id, source_id=second_source["id"], provider="greenhouse",
            tenant="gh-board", endpoint=second_source["endpoint"], strategy="PUBLIC_JSON",
            items=[item("gh-req-1001", "Systems Administrator")], profile_id="fixture",
        )
        after_retry = self.client.get("/profiles/fixture/audit-history").json()["events"]
        self.assertEqual([event for event in after_retry if event["type"] == "JOB_MERGED"], merged)

        # A distinct route remains a separate job and does not create a merge event.
        import_company_feed_jobs(
            self.store, company_id=company_id, source_id=second_source["id"], provider="greenhouse",
            tenant="gh-board", endpoint=second_source["endpoint"], strategy="PUBLIC_JSON",
            items=[item("gh-req-1002", "Systems Administrator") | {
                "url": "https://employer.example/careers/apply/req-1002"}], profile_id="fixture",
        )
        after_distinct = self.client.get("/profiles/fixture/audit-history").json()["events"]
        self.assertEqual([event for event in after_distinct if event["type"] == "JOB_MERGED"], merged)

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

    def test_preparation_queue_and_start_are_idempotent_profile_owned_job_events(self):
        from workers.dispatch import enqueue_operation
        from workers.operations import claim

        params = {"job_id": self.store.jobs("fixture")[0]["id"],
                  "tailor_fact_rewrites": [{"text": "private resume wording"}]}
        operation_id = enqueue_operation(self.store, "PREPARE", "fixture", params)
        self.assertEqual(enqueue_operation(self.store, "PREPARE", "fixture", params), operation_id)
        self.assertIsNotNone(claim(self.store, operation_id))

        response = self.client.get("/profiles/fixture/audit-history")
        self.assertEqual(response.status_code, 200, response.text)
        events = [event for event in response.json()["events"]
                  if event["type"] in {"APPLICATION_QUEUED", "APPLICATION_STARTED"}]
        self.assertEqual({event["type"] for event in events},
                         {"APPLICATION_QUEUED", "APPLICATION_STARTED"})
        self.assertTrue(all(event["subject"] for event in events))
        self.assertNotIn("private resume wording", response.text)
        self.assertFalse(any(event["type"] in {"APPLICATION_QUEUED", "APPLICATION_STARTED"}
                             for event in self.client.get(
                                 "/profiles/other/audit-history").json()["events"]))

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
