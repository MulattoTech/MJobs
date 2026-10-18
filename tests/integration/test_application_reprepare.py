"""Explicit stale-application reprepare preserves immutable revisions."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.tailor import target_fingerprint
from services.dedupe.routes import plan_application_routes, qualify_route_authority
from services.applications.preflight import persist_preflight
from services.applications.attempt_history import AttemptConflict
from services.applications.run_requests import (
    queue_mock_run, read_mock_run, validate_run_operation,
)
from tests.integration.test_spine import PROFILE


class ApplicationReprepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-application-reprepare-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'reprepare.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile = deepcopy(PROFILE)
        self.profile["answers"] = []
        self.store.save_profile(self.profile)
        other = deepcopy(PROFILE)
        other.update(id="other-profile", name="Other synthetic applicant",
                     email="other@example.invalid", answers=[])
        self.store.save_profile(other)
        with fixture_server() as fixture:
            discover(self.store, fixture, "fixture")
        self.job = next(row for row in self.store.jobs("fixture")
                        if row.get("requisition") == "B")
        self.application_id = prepare(self.store, self.job["id"], "fixture", self.root)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE",
        ), raise_server_exceptions=False)
        self.url = f"/profiles/fixture/applications/{self.application_id}/reprepare"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _payload(self, *, app=None, profile=None):
        app = app or self.store.application(self.application_id)
        profile = profile or self.store.profile("fixture")
        return {
            "expected_document_version": app["document_version"],
            "expected_application_profile_version": app["profile_version"],
            "expected_profile_version": profile["version"],
            "expected_state": app["state"],
        }

    def _operation(self, operation_id, profile_id, job_id, state="QUEUED"):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id, kind="PREPARE", entity_id=profile_id,
                params={"job_id": job_id, "profile_version": self.store.profile(profile_id)["version"],
                        "route_mode": "THIS_ROUTE", "route_id": self.store.application(
                            self.application_id)["route_id"], "tailor_resume": True,
                        "include_resume_contact": True, "cover_letter_style": "warm",
                        "cover_letter_length": "detailed", "request_id": operation_id},
                state=state, updated_at="2026-10-18T00:00:00Z",
            ))

    def test_explicit_reprepare_keeps_old_revision_audits_transition_and_is_idempotent(self):
        old_app = self.store.application(self.application_id)
        old_documents = application_documents(self.store, self.application_id, self.root)
        persist_preflight(self.store, self.application_id, {
            "result": "FAIL", "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
            "observed_at": "2026-10-18T00:00:00Z",
            "profile_version": old_app["profile_version"],
            "document_version": old_app["document_version"],
            "route_id": old_app["route_id"], "provider": "mock-ats",
            "tenant": "fixture", "requisition": "B", "metadata_fingerprint": "b" * 64,
            "mappings": [], "missing": [],
        })
        current = self.store.profile("fixture")
        self.store.save_profile({**current, "facts": [*current["facts"], {
            "id": "newly-approved-fact", "text": "A newly approved synthetic fact.",
            "kind": "experience", "source": "User-reviewed fixture",
            "verified": True, "user_approved": True, "confidence": 95,
        }], "answers": [{
            "id": "reviewed-answer", "question": "Describe a synthetic review.",
            "text": "Reviewed synthetic answer.", "source": "User-reviewed fixture",
            "verified": True, "user_approved": True, "confidence": 90,
        }]})
        latest = self.store.profile("fixture")
        payload = self._payload(app=old_app, profile=latest)
        response = self.client.post(self.url, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        refreshed = response.json()
        self.assertEqual(refreshed["id"], self.application_id)
        self.assertEqual(refreshed["profile_version"], latest["version"])
        self.assertEqual(refreshed["document_version"], old_app["document_version"] + 1)
        self.assertEqual(refreshed["question_preflight"]["result"], "FAIL")
        self.assertFalse(refreshed["question_preflight"]["is_current"])
        current_resume = next(row for row in application_documents(
            self.store, self.application_id, self.root
        ) if row["version"] == refreshed["document_version"] and row["kind"] == "resume")
        self.assertNotIn("A newly approved synthetic fact.", str(current_resume["facts_used"]))

        all_documents = application_documents(self.store, self.application_id, self.root)
        self.assertTrue(set(row["id"] for row in old_documents) <=
                        set(row["id"] for row in all_documents))
        old_revision = [row for row in all_documents
                        if row["version"] == old_app["document_version"]]
        self.assertEqual({row["profile_version"] for row in old_revision},
                         {old_app["profile_version"]})
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings())
        transition = events[-1]["payload"].get("reprepare")
        self.assertEqual(transition["prior"]["profile_version"], old_app["profile_version"])
        self.assertEqual(transition["prior"]["document_version"], old_app["document_version"])
        self.assertEqual(transition["prior"]["route_id"], old_app["route_id"])
        self.assertEqual(transition["current"]["profile_version"], latest["version"])

        event_count = len(events)
        documents_before_repeat = len(all_documents)
        repeated = self.client.post(self.url, json=self._payload())
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["document_version"], refreshed["document_version"])
        self.assertEqual(len(application_documents(self.store, self.application_id, self.root)),
                         documents_before_repeat)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            )).all().__len__(), event_count)

    def test_stale_cross_profile_and_inflight_reprepare_fail_closed(self):
        app = self.store.application(self.application_id)
        profile = self.store.profile("fixture")
        wrong_application_version = self.client.post(self.url, json={
            **self._payload(app=app, profile=profile),
            "expected_document_version": app["document_version"] + 10,
        })
        self.assertEqual(wrong_application_version.status_code, 409)
        other = self.client.post(
            f"/profiles/other-profile/applications/{self.application_id}/reprepare",
            json=self._payload(app=app, profile=profile),
        )
        self.assertEqual(other.status_code, 404)

        changed = deepcopy(profile)
        changed["name"] = "Concurrent synthetic profile edit"
        self.store.save_profile(changed)
        current = self.store.profile("fixture")
        stale_profile = self.client.post(self.url, json=self._payload(
            app=app, profile=profile))
        self.assertEqual(stale_profile.status_code, 409)
        self.assertEqual(current["version"], self.store.profile("fixture")["version"])

        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(
                id="synthetic-running-attempt", application_id=self.application_id,
                state="RUNNING", started_at="2026-10-18T00:00:00Z", steps=[],
            ))
        active = self.client.post(self.url, json=self._payload(app=app, profile=current))
        self.assertEqual(active.status_code, 409)
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         app["document_version"])

    def test_queued_prepare_is_scoped_to_profile_and_job(self):
        app = self.store.application(self.application_id)
        profile = self.store.profile("fixture")
        self._operation("queued-same-application", "fixture", self.job["id"])
        active = self.client.post(self.url, json=self._payload(app=app, profile=profile))
        self.assertEqual(active.status_code, 409, active.text)
        self.assertEqual(active.json()["detail"]["code"],
                         "APPLICATION_REPREPARE_IN_PROGRESS")

        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == "queued-same-application").values(state="CANCELLED"))
        self._operation("queued-different-job", "fixture", "f" * 64)
        self._operation("queued-different-profile", "other-profile", self.job["id"])
        allowed = self.client.post(self.url, json=self._payload(app=app, profile=profile))
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_nondefault_preparation_options_are_preserved_on_reprepare(self):
        profile = self.store.profile("fixture")
        profile["facts"].append({
            "id": "synthetic-experience", "text": "Built a synthetic review workflow.",
            "kind": "experience", "source": "User-reviewed fixture",
            "verified": True, "user_approved": True, "confidence": 95,
        })
        self.store.save_profile(profile)
        profile = self.store.profile("fixture")
        fact = next(row for row in profile["facts"] if row["fact_id"] == "synthetic-experience")
        application_id = prepare(
            self.store, self.job["id"], "fixture", self.root,
            profile_version=profile["version"], route_mode="THIS_ROUTE",
            route_id=self.store.application(self.application_id)["route_id"],
            tailor_resume=True, tailor_fact_ids=[fact["fact_id"]],
            tailor_fact_rewrites=[{"fact_id": fact["fact_id"],
                                   "text": "Reviewed synthetic rewrite of an approved fact.",
                                   "reviewed": True}],
            include_resume_contact=True, cover_letter_style="warm",
            cover_letter_length="detailed",
        )
        original_documents = application_documents(self.store, application_id, self.root)
        original_resume = next(row for row in original_documents if row["kind"] == "resume")
        app = self.store.application(application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == application_id
            ).values(state="REVIEW_REQUIRED"))
        response = self.client.post(self.url, json=self._payload(app=self.store.application(application_id),
                                                                 profile=profile))
        self.assertEqual(response.status_code, 200, response.text)
        documents = application_documents(self.store, application_id, self.root)
        current_revision = [row for row in documents
                            if row["version"] == response.json()["document_version"]]
        resume = next(row for row in current_revision if row["kind"] == "resume")
        letter = next(row for row in current_revision if row["kind"] == "cover-letter")
        self.assertEqual(resume["template"], original_resume["template"])
        self.assertEqual(resume["fact_ids"], [fact["fact_id"]])
        self.assertEqual(resume["generation_context"]["fact_rephrases"][0]["text"],
                         "Reviewed synthetic rewrite of an approved fact.")
        self.assertIn("contact_header", resume["generation_context"])
        self.assertEqual(letter["generation_context"]["style"], "warm")
        self.assertEqual(letter["generation_context"]["length"], "detailed")

        unchanged_repeat = self.client.post(
            self.url, json=self._payload(app=self.store.application(application_id),
                                         profile=profile))
        self.assertEqual(unchanged_repeat.status_code, 200, unchanged_repeat.text)
        self.assertEqual(unchanged_repeat.json()["document_version"],
                         response.json()["document_version"])

        latest = self.store.profile("fixture")
        self.store.save_profile({**latest, "name": "Updated synthetic applicant"})
        changed_profile = self.store.profile("fixture")
        stale_rewrite = self.client.post(
            self.url, json=self._payload(app=self.store.application(application_id),
                                         profile=changed_profile))
        self.assertEqual(stale_rewrite.status_code, 409, stale_rewrite.text)
        self.assertEqual(stale_rewrite.json()["detail"]["code"],
                         "APPLICATION_REPREPARE_REVIEW_REQUIRED")
        self.assertEqual(self.store.application(application_id)["document_version"],
                         response.json()["document_version"])

    def test_completed_old_run_receipt_cannot_certify_reprepared_revision(self):
        old_app = self.store.application(self.application_id)
        persist_preflight(self.store, self.application_id, {
            "result": "PASS", "reason": "SYNTHETIC_OLD_REVISION_PASS",
            "observed_at": "2026-10-18T00:00:00Z",
            "profile_version": old_app["profile_version"],
            "document_version": old_app["document_version"],
            "route_id": old_app["route_id"], "provider": "mock-ats",
            "tenant": "fixture", "requisition": "B",
            "metadata_fingerprint": "c" * 64,
            "mappings": [], "missing": [],
        })
        old_receipt = queue_mock_run(
            self.store, "fixture", self.application_id, submit=False,
            request_id="old-completed-dryrun",
        )
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == old_receipt["operation_id"]
            ).values(state="RUNNING"))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id
            ).values(state="DRY_RUN_COMPLETE"))
            connection.execute(update(m.operations).where(
                m.operations.c.id == old_receipt["operation_id"]
            ).values(state="COMPLETED"))

        completed_app = self.store.application(self.application_id)
        current = self.store.profile("fixture")
        self.store.save_profile({**current, "answers": [{
            "id": "new-reviewed-answer", "question": "Synthetic current question?",
            "text": "Current approved synthetic response.",
            "source": "User-reviewed fixture", "verified": True,
            "user_approved": True, "confidence": 90,
        }]})
        latest = self.store.profile("fixture")
        resumed = self.client.post(self.url, json=self._payload(
            app=completed_app, profile=latest))
        self.assertEqual(resumed.status_code, 200, resumed.text)
        current_app = resumed.json()
        self.assertEqual(current_app["state"], "PREPARED")
        self.assertGreater(current_app["document_version"], old_app["document_version"])
        self.assertEqual(current_app["profile_version"], latest["version"])
        self.assertEqual(current_app["question_preflight"]["result"], "PASS")
        self.assertFalse(current_app["question_preflight"]["is_current"])

        # The terminal old receipt remains readable with its original fingerprint,
        # while replaying that identity against new pins is rejected.
        old_readback = read_mock_run(
            self.store, "fixture", self.application_id, "old-completed-dryrun",
        )
        self.assertEqual(old_readback["state"], "COMPLETED")
        self.assertEqual(old_readback["input_fingerprint"],
                         old_receipt["input_fingerprint"])
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_REQUEST_REUSED"):
            queue_mock_run(
                self.store, "fixture", self.application_id, submit=False,
                request_id="old-completed-dryrun",
            )

        # A new operation freezes the current profile/document pins and must run
        # to create a replacement preflight; no old receipt is reused.
        new_run = queue_mock_run(
            self.store, "fixture", self.application_id, submit=False,
            request_id="new-revision-dryrun",
        )
        self.assertNotEqual(new_run["input_fingerprint"], old_receipt["input_fingerprint"])
        with self.store.engine.connect() as connection:
            new_operation = connection.execute(select(m.operations).where(
                m.operations.c.id == new_run["operation_id"],
            )).mappings().one()
        self.assertEqual(new_operation["params"]["input_snapshot"]["profile_version"],
                         latest["version"])
        self.assertEqual(new_operation["params"]["input_snapshot"]["document_version"],
                         current_app["document_version"])
        validate_run_operation(self.store, dict(new_operation))
        self.assertIsNone(self.store.application(self.application_id)["confirmation"])
        historical_preflight = self.client.get(
            f"/profiles/fixture/applications/{self.application_id}"
        ).json()["question_preflight"]
        self.assertEqual(historical_preflight["result"], "PASS")
        self.assertFalse(historical_preflight["is_current"])

    def test_generation_failure_keeps_old_pins_documents_and_audit(self):
        old_app = self.store.application(self.application_id)
        old_documents = application_documents(self.store, self.application_id, self.root)
        profile = self.store.profile("fixture")
        self.store.save_profile({**profile, "name": "Updated synthetic name"})
        latest = self.store.profile("fixture")
        old_audits = self.store.count("audit_events")
        with patch("services.documents.generate.publish_directory",
                   side_effect=OSError("synthetic publication failure")):
            response = self.client.post(self.url, json=self._payload(
                app=old_app, profile=latest))
        self.assertEqual(response.status_code, 500)
        current = self.store.application(self.application_id)
        self.assertEqual(current["profile_version"], old_app["profile_version"])
        self.assertEqual(current["document_version"], old_app["document_version"])
        self.assertEqual(application_documents(self.store, self.application_id, self.root),
                         old_documents)
        self.assertEqual(self.store.count("audit_events"), old_audits)

    def test_legacy_snapshot_without_saved_options_requires_review(self):
        app = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            event = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())).mappings().first()
            payload = dict(event["payload"])
            payload["preparation_config"] = {"route_mode": "DEFAULT"}
            connection.execute(update(m.audit).where(
                m.audit.c.id == event["id"],
            ).values(payload=payload))
        response = self.client.post(self.url, json=self._payload(app=app))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"],
                         "APPLICATION_REPREPARE_CONFIG_REQUIRED")
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         app["document_version"])

    def test_unversioned_completed_prepare_cannot_override_page_fit_revision_recipe(self):
        profile = self.store.profile("fixture")
        self._operation("older-completed-prepare", "fixture", self.job["id"], state="COMPLETED")
        application_id = prepare(
            self.store, self.job["id"], "fixture", self.root,
            profile_version=profile["version"], tailor_resume=True,
            resume_page_target=1,
            resume_page_fit_job_fingerprint=target_fingerprint(self.job),
        )
        app = self.store.application(application_id)
        fitted_resume = next(row for row in application_documents(
            self.store, application_id, self.root
        ) if row["version"] == app["document_version"] and row["kind"] == "resume")
        self.assertEqual(fitted_resume["generation_context"]["page_fit"]["target_page_count"], 1)
        with self.store.engine.begin() as connection:
            event = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())).mappings().first()
            payload = dict(event["payload"])
            payload.pop("preparation_config", None)
            connection.execute(update(m.audit).where(
                m.audit.c.id == event["id"],
            ).values(payload=payload))
            connection.execute(update(m.applications).where(
                m.applications.c.id == application_id,
            ).values(state="REVIEW_REQUIRED"))

        response = self.client.post(
            self.url, json=self._payload(app=self.store.application(application_id), profile=profile),
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"],
                         "APPLICATION_REPREPARE_CONFIG_REQUIRED")
        self.assertEqual(self.store.application(application_id)["document_version"],
                         app["document_version"])

    def test_default_route_is_pinned_when_higher_authority_route_appears(self):
        original = self.store.application(self.application_id)
        profile = self.store.profile("fixture")
        with self.store.engine.begin() as connection:
            connection.execute(m.company_domains.insert().values(
                id="verified-employer-domain", company_id=self.job["company_id"],
                domain="verified-employer.example",
                confidence=1.0, verified=True, verification_method="USER_REVIEWED_FIXTURE",
                first_seen_at="2026-10-18T00:00:00Z", last_verified_at="2026-10-18T00:00:00Z",
                evidence=["synthetic reviewed employer identity"],
            ))
            connection.execute(m.routes.insert().values(
                id="new-higher-authority-route", job_id=self.job["id"],
                company_id=self.job["company_id"], provider="employer", tenant="fixture",
                requisition=self.job["requisition"],
                url="https://verified-employer.example/apply",
            ))
            candidates = [dict(row) for row in connection.execute(
                select(m.routes).where(m.routes.c.job_id == self.job["id"])
            ).mappings()]
            qualified = qualify_route_authority(connection, candidates)
        self.assertEqual(next(route["authority"] for route in qualified
                              if route["id"] == "new-higher-authority-route"),
                         "VERIFIED_EMPLOYER")
        self.assertEqual(plan_application_routes(qualified, "DEFAULT")["selected"][0]
                         ["preferred_route"]["id"], "new-higher-authority-route")
        latest_profile = {**profile, "answers": [{
            "id": "reviewed-answer-after-discovery",
            "question": "What is a synthetic review answer?",
            "text": "This answer was reviewed after route discovery.",
            "source": "User-reviewed fixture", "verified": True,
            "user_approved": True, "confidence": 90,
        }]}
        self.store.save_profile(latest_profile)
        latest_profile = self.store.profile("fixture")

        with TestClient(self.client.app, raise_server_exceptions=True) as client:
            response = client.post(
                self.url, json=self._payload(app=original, profile=latest_profile),
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["route_id"], original["route_id"])
        self.assertEqual(self.store.application(self.application_id)["route_id"],
                         original["route_id"])
        with self.store.engine.connect() as connection:
            latest_event = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())).mappings().first()
        saved_config = latest_event["payload"]["preparation_config"]
        self.assertEqual(saved_config["resolved_route_id"], original["route_id"])
        self.assertEqual(saved_config["route_mode"], "THIS_ROUTE")
        self.assertEqual(saved_config["route_id"], original["route_id"])


if __name__ == "__main__":
    unittest.main()
