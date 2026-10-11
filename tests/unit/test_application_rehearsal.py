"""Local, value-safe application rehearsal behavior."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from services.applications.rehearsal import RehearsalError, build_rehearsal


class ApplicationRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'rehearsal.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = copy.deepcopy(PROFILE)
        profile["email"] = "alex@example.test"
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
                         route_id=None, profile_id="fixture", provider="greenhouse"):
        analysis = {"status": status, "reason": reason, "fields": fields}
        payload = {
            "operation_id": "synthetic-form-review-operation",
            "request_id": "synthetic-form-review-request",
            "input_fingerprint": "a" * 64,
            "profile_id": profile_id,
            "route_id": route_id or self.application["route_id"],
            "provider": provider,
            "analysis": analysis,
        }
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert().values(
                id=f"review-{len(fields)}-{status}-{reason or 'ok'}",
                entity_id=self.application_id,
                action="APPLICATION_FORM_REVIEW",
                payload=payload,
                timestamp=now(),
            ))

    @staticmethod
    def field(name, field_type="text", *, required=True, review_reason=None):
        return {"id": name, "name": name, "type": field_type,
                "required": required, "review_reason": review_reason}

    def test_populates_explicit_contact_and_exact_approved_answer_only(self):
        self.persist_analysis([
            self.field("email", "email"),
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
