"""Document generation history describes committed events, not current artifacts."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from tests.integration import test_lever_form_review_flow as postgres_fixture


class DocumentGenerationAuditTests(unittest.TestCase):
    def _new_store(self):
        self.database_url = f"sqlite:///{(self.root / 'history.db').as_posix()}"
        store = Store(self.database_url)
        store.migrate()
        return store

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-document-audit-")
        self.root = Path(self.temp.name)
        self.store = self._new_store()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        self.client = self._client()

    def _client(self):
        return TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                     operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def history(self, profile="fixture"):
        response = self.client.get(f"/profiles/{profile}/audit-history")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["events"]

    def document_events(self):
        return [event for event in self.history() if event["type"] in {
            "RESUME_GENERATED", "COVER_LETTER_GENERATED", "DOCUMENTS_PREPARED"}]

    def prepared_rows(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings().all()

    def test_preparation_commits_exact_kinds_and_displays_both_documents(self):
        rows = self.prepared_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"].get("document_kinds"), ["cover-letter", "resume"])
        events = self.document_events()
        self.assertEqual({event["type"] for event in events},
                         {"RESUME_GENERATED", "COVER_LETTER_GENERATED"})
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["timestamp"] == rows[0]["timestamp"] for event in events))

    def test_document_mutation_and_removal_cannot_change_committed_history(self):
        before = self.document_events()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(
                m.documents.c.application_id == self.application_id).values(kind="UNKNOWN"))
        self.assertEqual(self.document_events(), before)
        with self.store.engine.begin() as connection:
            connection.execute(delete(m.documents).where(
                m.documents.c.application_id == self.application_id))
        self.assertEqual(self.document_events(), before)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self._client()
        self.assertEqual(self.document_events(), before)
        self.assertFalse(any(event["type"] in {
            "RESUME_GENERATED", "COVER_LETTER_GENERATED", "DOCUMENTS_PREPARED"}
            for event in self.history("other")))

    def test_repreparation_does_not_rewrite_prior_snapshot_or_duplicate_reads(self):
        first = self.prepared_rows()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(m.audit.c.id == first["id"]).values(
                payload={**first["payload"], "document_kinds": ["resume"]}))
        prepare(self.store, self.job_id, "fixture", self.root, cover_letter_style="warm")
        events = self.document_events()
        self.assertEqual(len(events), 3)
        old = [event for event in events if event["id"].startswith(first["id"] + ":")]
        self.assertEqual([event["type"] for event in old], ["RESUME_GENERATED"])
        self.assertEqual(self.document_events(), events)
        self.assertEqual(len(self.prepared_rows()), 2)

    def test_legacy_unknown_snapshot_keeps_generic_event_without_guessing_kinds(self):
        row = self.prepared_rows()[0]
        payload = dict(row["payload"])
        payload.pop("document_kinds", None)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(m.audit.c.id == row["id"]).values(payload=payload))
        events = self.document_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], row["id"])
        self.assertEqual(events[0]["type"], "DOCUMENTS_PREPARED")
        self.assertIn("not recorded", events[0]["summary"])
        with self.store.engine.begin() as connection:
            connection.execute(delete(m.documents).where(
                m.documents.c.application_id == self.application_id))
        self.assertEqual(self.document_events(), events)

    def test_failure_after_audit_insert_rolls_back_artifacts_and_history(self):
        before = self.history()
        rows = self.prepared_rows()
        prior_files = {path: path.read_bytes() for path in (self.root / self.application_id).rglob("*")
                       if path.is_file()}
        application = self.store.application(self.application_id)
        next_revision = self.root / self.application_id / application["profile_version"] / "2"
        original = self.store._audit

        def fail_after_insert(connection, entity_id, action, payload):
            original(connection, entity_id, action, payload)
            if action == "DOCUMENTS_PREPARED":
                raise RuntimeError("Synthetic post-audit failure")

        with patch.object(self.store, "_audit", side_effect=fail_after_insert), self.assertRaises(RuntimeError):
            prepare(self.store, self.job_id, "fixture", self.root, cover_letter_style="warm")
        self.assertEqual(self.history(), before)
        self.assertEqual(self.prepared_rows(), rows)
        self.assertEqual(self.store.application(self.application_id)["document_version"], 1)
        self.assertFalse(next_revision.exists())
        self.assertEqual({path: path.read_bytes() for path in prior_files}, prior_files)
        prepare(self.store, self.job_id, "fixture", self.root, cover_letter_style="warm")
        self.assertEqual(len(self.prepared_rows()), 2)
        self.assertEqual(len(self.document_events()), 4)


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"), "Requires isolated disposable PostgreSQL")
class DocumentGenerationAuditPostgresTests(DocumentGenerationAuditTests):
    _new_store = postgres_fixture.LeverFormReviewPostgresFlowTests._new_store
    _drop_schema = postgres_fixture.LeverFormReviewPostgresFlowTests._drop_schema
