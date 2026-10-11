"""Local, value-safe application rehearsal behavior."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from services.applications.rehearsal import RehearsalError, build_rehearsal
from workers.application_form_review import handle_form_review


class ApplicationRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'rehearsal.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = copy.deepcopy(PROFILE)
        profile["email"] = "alex@example.test"
        profile["contact"] = {
            "first_name": "Alex", "middle_name": "Q", "last_name": "Fixture",
            "preferred_name": "Lex", "phones": ["+1 555 010 0200"],
            "address_line1": "12 Example Street", "address_line2": "Unit 3",
            "city": "Tulsa", "state": "OK", "postal_code": "74101",
            "country": "United States", "linkedin": "https://example.test/in/alex",
            "github": "https://example.test/alex", "portfolio": "https://example.test",
        }
        profile["answers"].append({
            "id": "weekends",
            "question": "Can you work weekends?",
            "text": "Yes, by prior arrangement.",
            "source": "fictional-test-profile",
            "verified": True,
            "user_approved": True,
            "confidence": 100,
        })
        self.store.save_profile(profile)
        self.profile_seed = copy.deepcopy(profile)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture")
                       if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.application = self.store.application(self.application_id)
        self.route_url = "https://boards.greenhouse.io/example/jobs/12345"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.application["route_id"],
            ).values(provider="greenhouse", tenant="example", requisition="12345",
                     url=self.route_url))
        self.application = self.store.application(self.application_id)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def persist_analysis(self, fields, *, status="ANALYZED", reason=None,
                         route_id=None, profile_id="fixture", provider="greenhouse",
                         route_url=None):
        analysis = {"status": status, "reason": reason, "fields": fields}
        route_url = route_url or self.route_url
        audit_id = f"review-{len(fields)}-{status}-{reason or 'ok'}"
        operation_id = f"operation-{audit_id}"
        request_id = "synthetic-form-review-request"
        frozen = {
            "profile_id": profile_id,
            "application_id": self.application_id,
            "route_id": route_id or self.application["route_id"],
            "route_url": route_url,
            "provider": provider,
            "profile_version": str(self.store.profile(profile_id)["version"])
            if profile_id == "fixture" else "profile-version-fixture",
        }
        input_fingerprint = hashlib.sha256(json.dumps(
            frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
        payload = {
            "operation_id": operation_id,
            "request_id": request_id,
            "input_fingerprint": input_fingerprint,
            "profile_id": profile_id,
            "route_id": route_id or self.application["route_id"],
            "provider": provider,
            "analysis": analysis,
        }
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id, kind="APPLICATION_FORM_REVIEW",
                entity_id=self.application_id,
                params={**frozen, "request_id": request_id,
                        "input_fingerprint": input_fingerprint},
                state="SUCCEEDED", error=None, updated_at=now(), attempt_count=1,
                cancel_requested=False,
            ))
            connection.execute(m.audit.insert().values(
                id=audit_id,
                entity_id=self.application_id,
                action="APPLICATION_FORM_REVIEW",
                payload=payload,
                timestamp=now(),
            ))

    def run_real_form_review(self):
        profile = self.store.profile("fixture")
        frozen = {
            "profile_id": "fixture",
            "application_id": self.application_id,
            "route_id": self.application["route_id"],
            "route_url": self.route_url,
            "provider": "greenhouse",
            "profile_version": str(profile["version"]),
        }
        request_id = "worker-generated-review-request"
        fingerprint = hashlib.sha256(json.dumps(
            frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
        params = {**frozen, "request_id": request_id,
                  "input_fingerprint": fingerprint}
        operation_id = identity("APPLICATION_FORM_REVIEW", self.application_id, params)
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert().values(
                id=operation_id, kind="APPLICATION_FORM_REVIEW",
                entity_id=self.application_id, params=params, state="RUNNING",
                error=None, updated_at=now(), attempt_count=1,
                cancel_requested=False,
                lease_expires_at=(datetime.now(timezone.utc)
                                  + timedelta(minutes=2)).isoformat(),
            ))
            operation = dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one())
        html = ('<form><label for="email">Email</label>'
                '<input id="email" name="email" type="email" required></form>')
        return handle_form_review(
            self.store, operation, fetch_html=lambda _url: html, check=lambda: None,
        )

    @staticmethod
    def field(name, field_type="text", *, required=True, review_reason=None):
        return {"id": name, "name": name, "type": field_type,
                "required": required, "review_reason": review_reason}

    def test_populates_explicit_contact_and_exact_approved_answer_only(self):
        self.persist_analysis([
            self.field("email", "email"),
            self.field("full_name"),
            self.field("first_name"),
            self.field("phone_number", "tel"),
            self.field("address_line1"),
            self.field("weekend_availability"),
            self.field("resume_upload", "file"),
            self.field("custom_question"),
        ])
        with self.store.engine.connect() as connection:
            audit_before = connection.execute(func.count(m.audit.c.id)).scalar_one()

        report = build_rehearsal(self.store, "fixture", self.application_id)

        self.assertEqual(report["profile_version"], self.store.profile("fixture")["version"])
        self.assertEqual(report["route_id"], self.application["route_id"])
        self.assertFalse(report["submission_enabled"])
        self.assertFalse(report["ready"])
        self.assertEqual(report["fields"], [
            {"key": "email", "label": "email", "type": "email", "required": True,
             "value": "alex@example.test", "source": "CONTACT", "reason_code": None},
            {"key": "full_name", "label": "full_name", "type": "text", "required": True,
             "value": "Alex Fixture", "source": "CONTACT", "reason_code": None},
            {"key": "first_name", "label": "first_name", "type": "text", "required": True,
             "value": "Alex", "source": "CONTACT", "reason_code": None},
            {"key": "phone_number", "label": "phone_number", "type": "tel", "required": True,
             "value": "+1 555 010 0200", "source": "CONTACT", "reason_code": None},
            {"key": "address_line1", "label": "address_line1", "type": "text", "required": True,
             "value": "12 Example Street", "source": "CONTACT", "reason_code": None},
            {"key": "weekend_availability", "label": "weekend_availability", "type": "text",
             "required": True, "value": "Yes, by prior arrangement.",
             "source": "VERIFIED_ANSWER", "reason_code": None},
            {"key": "resume_upload", "label": "resume_upload", "type": "file",
             "required": True, "value": "Resume document v1",
             "source": "DOCUMENT", "reason_code": None},
            {"key": "custom_question", "label": "custom_question", "type": "text",
             "required": True, "value": None, "source": "UNRESOLVED",
             "reason_code": "UNKNOWN_FIELD"},
        ])
        self.assertIn("REQUIRED_FIELD_UNRESOLVED", report["blockers"])
        serialized = json.dumps(report)
        for forbidden in ("review_reason", "options", "source_url", "PRIVATE_PAGE",
                          "resume/", "cover letter/"):
            self.assertNotIn(forbidden, serialized)
        with self.store.engine.connect() as connection:
            audit_after = connection.execute(func.count(m.audit.c.id)).scalar_one()
        self.assertEqual(audit_before, audit_after)

    def test_sensitive_controls_and_non_analyzed_forms_stay_unresolved(self):
        self.persist_analysis([
            self.field("password", "password", review_reason="CREDENTIAL_FIELD"),
        ], status="REVIEW_REQUIRED", reason="FORM_REQUIRES_HUMAN_REVIEW")

        report = build_rehearsal(self.store, "fixture", self.application_id)

        self.assertEqual(report["fields"][0]["value"], None)
        self.assertEqual(report["fields"][0]["source"], "UNRESOLVED")
        self.assertEqual(report["fields"][0]["reason_code"], "FORM_FIELD_REVIEW_REQUIRED")
        self.assertIn("FORM_REQUIRES_HUMAN_REVIEW", report["blockers"])
        self.assertIn("REQUIRED_FIELD_UNRESOLVED", report["blockers"])

    def test_challenge_protected_report_fails_closed_without_metadata(self):
        self.persist_analysis([], status="REVIEW_REQUIRED", reason="CHALLENGE_PROTECTED")

        report = build_rehearsal(self.store, "fixture", self.application_id)

        self.assertEqual(report["fields"], [])
        self.assertEqual(report["blockers"], ["CHALLENGE_PROTECTED"])
        self.assertFalse(report["ready"])

    def test_review_is_required_when_no_successful_persisted_analysis_exists(self):
        with self.assertRaises(RehearsalError) as raised:
            build_rehearsal(self.store, "fixture", self.application_id)
        self.assertEqual(raised.exception.code, "FORM_REVIEW_REQUIRED")
        self.assertEqual(str(raised.exception), "FORM_REVIEW_REQUIRED")

    def test_application_is_not_visible_to_another_profile(self):
        with self.assertRaises(RehearsalError) as raised:
            build_rehearsal(self.store, "other-profile", self.application_id)
        self.assertEqual(raised.exception.code, "APPLICATION_NOT_FOUND")
        self.assertEqual(str(raised.exception), "APPLICATION_NOT_FOUND")

    def test_review_bound_to_another_profile_or_route_is_stale(self):
        self.persist_analysis([self.field("email", "email")], profile_id="other-profile")

        with self.assertRaises(RehearsalError) as raised:
            build_rehearsal(self.store, "fixture", self.application_id)

        self.assertEqual(raised.exception.code, "FORM_REVIEW_STALE")

    def test_real_worker_receipt_is_stale_after_route_url_changes(self):
        worker_result = self.run_real_form_review()
        self.assertEqual(worker_result["analysis"]["status"], "ANALYZED")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.application["route_id"],
            ).values(url="https://boards.greenhouse.io/example/jobs/99999"))

        with self.assertRaises(RehearsalError) as raised:
            build_rehearsal(self.store, "fixture", self.application_id)

        self.assertEqual(raised.exception.code, "FORM_REVIEW_STALE")

    def test_real_worker_audit_receipt_builds_current_profile_rehearsal(self):
        worker_result = self.run_real_form_review()
        self.assertEqual(worker_result["analysis"]["status"], "ANALYZED")

        report = build_rehearsal(self.store, "fixture", self.application_id)

        self.assertEqual(report["fields"][0]["key"], "email")
        self.assertEqual(report["fields"][0]["value"], "alex@example.test")
        self.assertEqual(report["profile_version"], self.store.profile("fixture")["version"])
        self.assertTrue(report["fingerprint"])

    def test_real_worker_receipt_is_stale_after_profile_version_changes(self):
        worker_result = self.run_real_form_review()
        self.assertEqual(worker_result["analysis"]["status"], "ANALYZED")
        updated = copy.deepcopy(self.profile_seed)
        updated["contact"]["city"] = "Oklahoma City"
        self.store.save_profile(updated)

        with self.assertRaises(RehearsalError) as raised:
            build_rehearsal(self.store, "fixture", self.application_id)

        self.assertEqual(raised.exception.code, "FORM_REVIEW_STALE")

    def test_report_fingerprint_binds_route_and_field_metadata(self):
        self.persist_analysis([self.field("email", "email")])
        first = build_rehearsal(self.store, "fixture", self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id="second-review-metadata", entity_id=self.application_id,
                action="APPLICATION_FORM_REVIEW",
                payload={
                    "operation_id": "synthetic-later-operation",
                    "request_id": "synthetic-later-request",
                    "input_fingerprint": "b" * 64,
                    "profile_id": "fixture", "route_id": self.application["route_id"],
                    "provider": "greenhouse",
                    "analysis": {"status": "ANALYZED", "reason": None,
                                 "fields": [self.field("first_name")]},
                }, timestamp="9999-01-01T00:00:00+00:00",
            ))
        second = build_rehearsal(self.store, "fixture", self.application_id)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["fields"][0]["key"], "first_name")


if __name__ == "__main__":
    unittest.main()
