"""Profile-scoped, user-reported follow-up drafts stay local and read-only."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit, urlunsplit

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.tracking.applications import set_application_pipeline_status
from tests.integration.test_spine import PROFILE


class ApplicationFollowUpDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-follow-up-drafts-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'follow-up.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant",
                                 "email": "other@example.test"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.jobs = self.store.jobs("fixture")
        self.assertGreaterEqual(len(self.jobs), 2)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _prepare(self, profile_id, job):
        return prepare(self.store, job["id"], profile_id, self.root)

    def _set_status(self, profile_id, application_id, status, request_id):
        return self.client.put(
            f"/profiles/{profile_id}/applications/{application_id}/tracking/status",
            json={"status": status, "request_id": request_id},
        )

    def _backdate_status(self, profile_id, application_id, status, request_id, timestamp):
        event_id = identity("application-tracking-event", profile_id, application_id, request_id)
        with self.store.engine.begin() as connection:
            result = connection.execute(update(m.audit).where(
                m.audit.c.id == event_id,
                m.audit.c.action == "APPLICATION_PIPELINE_STATUS",
            ).values(timestamp=timestamp.isoformat()))
        self.assertEqual(result.rowcount, 1)

    def test_candidate_boundary_status_transitions_profile_isolation_and_read_only_get(self):
        from services.tracking.follow_up_drafts import draft_follow_up_candidates

        now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        quiet_app = self._prepare("fixture", self.jobs[0])
        applied = self._set_status("fixture", quiet_app, "APPLIED", "follow-up-applied")
        self.assertEqual(applied.status_code, 200, applied.text)
        self._backdate_status("fixture", quiet_app, "APPLIED", "follow-up-applied",
                              now - timedelta(days=7))
        with self.store.engine.begin() as connection:
            company_id = connection.execute(select(m.jobs.c.company_id).where(
                m.jobs.c.id == self.jobs[0]["id"])).scalar_one()
            connection.execute(update(m.jobs).where(
                m.jobs.c.id == self.jobs[0]["id"]).values(
                    title="Synthetic Admin recruiter@example.test +1 (555) 010-0199 "
                          "@synthetic_recruiter"))
            connection.execute(update(m.companies).where(
                m.companies.c.id == company_id).values(
                    name="Example Employer https://private.example.test/path"))
        self.client.post(
            f"/profiles/fixture/applications/{quiet_app}/tracking/notes",
            json={"text": "Synthetic history: contact recruiter@example.test or @synthetic_recruiter; "
                           "phone +1 (555) 010-0199; https://private.example.test/path",
                  "request_id": "follow-up-contact-scrub-note"},
        )

        # The durable user-reported APPLIED event is exactly on the threshold.
        result = draft_follow_up_candidates(self.store, "fixture", 7, now=now)
        self.assertEqual(result["quiet_days"], 7)
        self.assertEqual([row["application_id"] for row in result["candidates"]], [quiet_app])
        self.assertEqual(draft_follow_up_candidates(
            self.store, "fixture", 8, now=now)["candidates"], [])

        candidate = result["candidates"][0]
        self.assertEqual(candidate["job_id"], self.jobs[0]["id"])
        self.assertIn("Synthetic Admin", candidate["job_title"])
        self.assertIn("Example Employer", candidate["company"])
        self.assertEqual(candidate["days_quiet"], 7)
        self.assertFalse(candidate["verified"])
        self.assertTrue(candidate["reported_at"])
        self.assertIn("USER_REPORTED", candidate["attribution"])
        self.assertEqual(candidate["character_limits"], {
            "subject": 120, "email_body": 500, "dm_body": 280,
        })
        self.assertLessEqual(len(candidate["email_subject"]), 120)
        self.assertLessEqual(len(candidate["email_body"]), 500)
        self.assertLessEqual(len(candidate["dm_body"]), 280)
        combined_draft = "\n".join((candidate["email_subject"], candidate["email_body"],
                                     candidate["dm_body"]))
        for private_contact in ("alex@example.test", "recruiter@example.test",
                                "other@example.test", "+1 (555) 010-0199",
                                "https://private.example.test/path", "@synthetic_recruiter"):
            self.assertNotIn(private_contact, combined_draft)
            self.assertNotIn(private_contact, candidate["job_title"] + candidate["company"])
        # Attribution and verification remain visible as metadata; the draft text
        # itself contains only the short, copyable status-check wording.

        # A linked external report is aged from its explicit user-reported
        # applied_at value even though its APPLIED status event was just recorded.
        external = self.client.post("/applications/external", json={
            "profile_id": "fixture", "request_id": "follow-up-external-report",
            "company": "Synthetic External Employer", "title": "Synthetic External Role",
            "application_url": "https://apply.example.test/follow-up-role",
            "source": "MANUAL", "applied_at": "2026-07-10T12:00:00Z",
        })
        self.assertEqual(external.status_code, 201, external.text)
        self.assertEqual(external.json()["tracking"]["attribution"], "EXTERNAL_USER_REPORTED")
        external_id = external.json()["application_id"]
        external_status = next(event for event in external.json()["tracking"]["history"]
                               if event["type"] == "status")
        external_applied_at = datetime.fromisoformat(
            external.json()["tracking"]["external_reports"][0]["applied_at"].replace(
                "Z", "+00:00"))
        self.assertGreater(datetime.fromisoformat(
            external_status["timestamp"].replace("Z", "+00:00")), external_applied_at)
        at_ninety = draft_follow_up_candidates(self.store, "fixture", 90, now=now)
        external_candidate = next(row for row in at_ninety["candidates"]
                                  if row["application_id"] == external_id)
        self.assertEqual(external_candidate["attribution"], "EXTERNAL_USER_REPORTED")
        self.assertEqual(datetime.fromisoformat(
            external_candidate["reported_at"].replace("Z", "+00:00")),
            now - timedelta(days=90))
        self.assertEqual(external_candidate["days_quiet"], 90)
        other_app = self._prepare("other", self.jobs[0])
        self.assertEqual(self._set_status("other", other_app, "APPLIED", "other-applied").status_code, 200)
        self._backdate_status("other", other_app, "APPLIED", "other-applied",
                              now - timedelta(days=30))
        before_reads = self.store.count("audit_events")
        first = self.client.get("/profiles/fixture/application-follow-up-drafts?quiet_days=7")
        second = self.client.get("/profiles/fixture/application-follow-up-drafts?quiet_days=7")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertNotIn(other_app, {row["application_id"] for row in first.json()["candidates"]})
        self.assertEqual(self.store.count("audit_events"), before_reads)
        self.assertEqual(self.client.get(
            "/profiles/other/application-follow-up-drafts?quiet_days=7").status_code, 200)
        self.assertIn(other_app, {row["application_id"] for row in self.client.get(
            "/profiles/other/application-follow-up-drafts?quiet_days=7").json()["candidates"]})
        self.assertEqual(self.client.get(
            "/profiles/missing/application-follow-up-drafts?quiet_days=7").status_code, 404)

        for index, status in enumerate(("SAVED", "READY", "RECRUITER_CONTACT", "INTERVIEW",
                                        "TECHNICAL_INTERVIEW", "FINAL_INTERVIEW", "OFFER",
                                        "REJECTED", "WITHDRAWN")):
            changed = self._set_status("fixture", quiet_app, status,
                                       f"follow-up-status-transition-{index}")
            self.assertEqual(changed.status_code, 200, changed.text)
            response = self.client.get(
                "/profiles/fixture/application-follow-up-drafts?quiet_days=7")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(quiet_app, {
                row["application_id"] for row in response.json()["candidates"]
            }, f"{status} must suppress the former APPLIED candidate")

    def test_interval_validation_and_automation_only_submission_is_not_a_candidate(self):
        from services.applications.mock_adapter import apply_mock

        for invalid in (0, 91):
            response = self.client.get(
                f"/profiles/fixture/application-follow-up-drafts?quiet_days={invalid}")
            self.assertEqual(response.status_code, 422, response.text)
        for valid in (1, 90):
            response = self.client.get(
                f"/profiles/fixture/application-follow-up-drafts?quiet_days={valid}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["quiet_days"], valid)

        application_id = self._prepare("fixture", self.jobs[1])
        with fixture_server() as origin:
            application = self.store.application(application_id)
            with self.store.engine.begin() as connection:
                route = connection.execute(select(m.routes).where(
                    m.routes.c.id == application["route_id"])).mappings().one()
                old_url = urlsplit(route["url"])
                live_origin = urlsplit(origin)
                connection.execute(update(m.routes).where(
                    m.routes.c.id == application["route_id"]).values(
                        url=urlunsplit((live_origin.scheme, live_origin.netloc,
                                        old_url.path, old_url.query, old_url.fragment))))
            submitted = apply_mock(self.store, application_id, origin, self.root, submit=True)
        self.assertEqual(submitted["state"], "CONFIRMED")
        attempts_before_read = self.store.count("application_attempts")
        self.assertEqual(attempts_before_read, 1)
        response = self.client.get(
            "/profiles/fixture/application-follow-up-drafts?quiet_days=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(application_id, {
            row["application_id"] for row in response.json()["candidates"]
        }, "automation-only confirmation is not user-reported follow-up history")
        self.assertEqual(self.store.count("worker_operations"), 0)
        self.assertEqual(self.store.count("application_attempts"), attempts_before_read)

    def test_profile_error_and_candidate_store_reopen_do_not_need_mutating_route(self):
        application_id = self._prepare("fixture", self.jobs[0])
        self.assertEqual(self._set_status(
            "fixture", application_id, "APPLIED", "follow-up-reopen-applied").status_code, 200)
        self._backdate_status(
            "fixture", application_id, "APPLIED", "follow-up-reopen-applied",
            datetime.now(timezone.utc) - timedelta(days=2))
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(self.root / 'follow-up.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        response = self.client.get(
            "/profiles/fixture/application-follow-up-drafts?quiet_days=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(application_id, {
            row["application_id"] for row in response.json()["candidates"]
        })
        self.assertEqual(self.client.post(
            "/profiles/fixture/application-follow-up-drafts?quiet_days=1").status_code, 405)
