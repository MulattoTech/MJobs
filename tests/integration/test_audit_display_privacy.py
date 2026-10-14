"""Known secret markers in persisted labels must not reach Activity HTML/API."""
import os
import unittest

from sqlalchemy import select, update

from packages.persistence import models as m
from services.audit_history import _event
from tests.integration import test_document_generation_audit as fixture
from tests.integration import test_lever_form_review_flow as postgres_fixture


class AuditDisplayPrivacyTests(unittest.TestCase):
    _new_store = fixture.DocumentGenerationAuditTests._new_store
    setUp = fixture.DocumentGenerationAuditTests.setUp
    _client = fixture.DocumentGenerationAuditTests._client
    tearDown = fixture.DocumentGenerationAuditTests.tearDown
    history = fixture.DocumentGenerationAuditTests.history
    document_events = fixture.DocumentGenerationAuditTests.document_events
    prepared_rows = fixture.DocumentGenerationAuditTests.prepared_rows

    def test_known_secret_markers_and_urls_in_labels_are_withheld_without_mutating_data(self):
        for label in (
            "Engineer api_key=SYNTHETIC_PRIVATE_KEY",
            'Engineer password: "SYNTHETIC PRIVATE VALUE"',
            "Engineer ACCESS-TOKEN = SYNTHETIC_PRIVATE_TOKEN",
            "Engineer client_secret=SYNTHETIC_PRIVATE_CLIENT",
            "Engineer private-key: SYNTHETIC_PRIVATE_KEY",
            "Engineer access_key=SYNTHETIC_PRIVATE_ACCESS",
            "Engineer AWS_SECRET_ACCESS_KEY=SYNTHETIC_PRIVATE_AWS",
            "Engineer AWS_ACCESS_KEY_ID=SYNTHETIC_PRIVATE_AWS_ID",
            "Engineer oauth_refresh_token=SYNTHETIC_PRIVATE_OAUTH",
            "Engineer JWT_SECRET=SYNTHETIC_PRIVATE_JWT",
            'Engineer {"client_secret":"SYNTHETIC_PRIVATE_JSON"}',
            "Engineer Authorization: Bearer SYNTHETIC_PRIVATE_BEARER",
            "Engineer Bearer SYNTHETIC_PRIVATE_BEARER",
            "Engineer https://user:SYNTHETIC_PRIVATE_PASSWORD@example.invalid/",
            "Engineer https://example.invalid/?credential=SYNTHETIC_PRIVATE_QUERY",
        ):
            with self.subTest(label=label), self.store.engine.begin() as connection:
                company_id = connection.execute(select(m.jobs.c.company_id).where(
                    m.jobs.c.id == self.job_id)).scalar_one()
                connection.execute(update(m.jobs).where(m.jobs.c.id == self.job_id).values(title=label))
                connection.execute(update(m.companies).where(m.companies.c.id == company_id).values(name=label))
            response = self.client.get("/profiles/fixture/audit-history")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("SYNTHETIC", response.text)
            events = response.json()["events"]
            self.assertTrue(any(event["type"] == "RESUME_GENERATED" for event in events))
            self.assertTrue(all(event["subject"] == "Details withheld" and event["company"] == ""
                                for event in events if event["type"] == "RESUME_GENERATED"))
            with self.store.engine.connect() as connection:
                self.assertEqual(connection.execute(select(m.jobs.c.title).where(
                    m.jobs.c.id == self.job_id)).scalar_one(), label)

    def test_normal_labels_keep_meaning_and_projection_remains_bounded(self):
        for label, expected in (("Secret Service Data Engineer", "Secret Service Data Engineer"),
                                ("Token Platform Engineer", "Token Platform Engineer"),
                                ("Data Engineer", "Data Engineer"),
                                ("x" * 500, "x" * 500)):
            with self.subTest(label=label[:40]), self.store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == self.job_id).values(title=label))
            self.assertTrue(all(event["subject"] == expected for event in self.document_events()))
        # PostgreSQL rejects >500 characters at storage admission; SQLite does
        # not. Exercise the projection's defensive limit without making an
        # invalid PostgreSQL fixture or weakening its schema constraint.
        projected = _event({"id": "label-limit", "timestamp": "2026-10-14T00:00:00Z"},
                           "JOB_DISCOVERED", "Job added", "x" * 501, "x" * 501)
        self.assertEqual(projected["subject"], "Details withheld")
        self.assertEqual(projected["company"], "")

    def test_unknown_private_document_kinds_and_payload_fields_never_reach_history(self):
        row = self.prepared_rows()[0]
        for kinds in (None, {}, "resume", [], ["RESUME_SYNTHETIC_PRIVATE"],
                      ["resume", {"api_key": "SYNTHETIC_PRIVATE"}], ["resume"] * 100):
            with self.subTest(kinds=kinds), self.store.engine.begin() as connection:
                connection.execute(update(m.audit).where(m.audit.c.id == row["id"]).values(payload={
                    "document_kinds": kinds, "api_key": "SYNTHETIC_PRIVATE_KEY",
                    "document_text": "SYNTHETIC_PRIVATE_DOCUMENT", "file": "/private/profile.pdf",
                }))
            response = self.client.get("/profiles/fixture/audit-history")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("SYNTHETIC_PRIVATE", response.text)
            self.assertNotIn("/private/", response.text)
            documents = self.document_events()
            self.assertEqual([event["type"] for event in documents], ["DOCUMENTS_PREPARED"])
            self.assertIn("unavailable", documents[0]["summary"])
            self.assertEqual(set(documents[0]), {"id", "type", "summary", "subject", "company", "timestamp"})


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "Requires isolated disposable PostgreSQL")
class AuditDisplayPrivacyPostgresTests(AuditDisplayPrivacyTests):
    _new_store = postgres_fixture.LeverFormReviewPostgresFlowTests._new_store
    _drop_schema = postgres_fixture.LeverFormReviewPostgresFlowTests._drop_schema
