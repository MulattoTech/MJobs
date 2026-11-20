"""Archive authority must survive bounded history views and old retries."""
from datetime import datetime, timedelta
import hashlib
import json
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.document_archive import create_document_archive_router, document_archive_metadata
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.applications.supporting_documents import (
    SupportingDocumentError,
    list_selection,
    select_documents,
)
from services.documents.archive import (
    archive_document,
    document_archive_state,
    restore_document,
)


class DocumentArchiveHistoryWindowTests(unittest.TestCase):
    def setUp(self):
        # A named memory database keeps the real Store configuration and lets
        # the router's worker thread read the same database without disk I/O.
        self.store = Store(
            f"sqlite:///file:archive-history-{uuid4().hex}?mode=memory&cache=shared&uri=true"
        )
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.applications = {}
        for profile_id in ("fixture", "other"):
            self.store.save_profile({
                "id": profile_id, "name": "Synthetic archive fixture",
                "email": f"{profile_id}@example.test", "facts": [], "answers": [],
            })
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="synthetic-company", name="Synthetic company", domain="example.test",
            ))

    def _application(self, profile_id="fixture", name="main"):
        key = (profile_id, name)
        if key in self.applications:
            return self.applications[key]
        application_id = f"{profile_id}-{name}-application"
        job_id = f"{profile_id}-{name}-job"
        route_id = f"{profile_id}-{name}-route"
        profile_version = self.store.profile(profile_id)["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id="synthetic-company", requisition=job_id,
                title="Synthetic job", description="Synthetic description", skills=[],
                location="Synthetic location", remote_type="REMOTE", attributes={},
            ))
            connection.execute(m.routes.insert().values(
                id=route_id, job_id=job_id, company_id="synthetic-company", provider="mock",
                tenant="synthetic", requisition=job_id, url="https://example.test/apply",
            ))
            connection.execute(m.applications.insert().values(
                id=application_id, job_id=job_id, route_id=route_id, profile_id=profile_id,
                profile_version=profile_version, state="PREPARED", target="MOCK",
                fact_ids=[], trace=[], document_version=1,
                created_at="2026-01-01T00:00:00+00:00",
            ))
        self.applications[key] = application_id
        return application_id

    def _revision(self, scope, name, *, profile_id="fixture", document_kind=None):
        document_id = f"{profile_id}-{scope}-{name}"
        digest = hashlib.sha256(document_id.encode()).hexdigest()
        timestamp = "2026-01-01T00:00:00+00:00"
        application_id = self._application(profile_id) if scope == "generated" else None
        with self.store.engine.begin() as connection:
            if scope == "generated":
                connection.execute(m.documents.insert().values(
                    id=document_id, application_id=application_id,
                    kind=document_kind or "resume", version=1,
                    profile_version=self.store.profile(profile_id, connection=connection)["version"],
                    files={"txt": f"{document_id}.txt"}, fact_ids=[], sha256={"txt": digest},
                    template="synthetic-archive-fixture", created_at=timestamp,
                ))
            else:
                upload_id = f"{document_id}-upload"
                connection.execute(m.uploaded_resumes.insert().values(
                    id=upload_id, profile_id=profile_id, filename=f"{name}.txt", format="txt",
                    media_type="text/plain", size_bytes=1, sha256=digest,
                    status="STORED_UNREVIEWED", created_at=timestamp,
                ))
                connection.execute(m.resume_revisions.insert().values(
                    id=document_id, profile_id=profile_id, upload_id=upload_id,
                    series_id=f"{document_id}-series", label=name, version=1,
                    created_at=timestamp, document_kind=document_kind or "BASE_RESUME",
                ))
        return document_id, application_id

    def _archive(self, document_id, operation_id, *, profile_id="fixture"):
        return archive_document(
            self.store, profile_id=profile_id, document_id=document_id,
            operation_id=operation_id,
        )

    def _later_transitions(self, original, count):
        """Batch valid alternating history; exercise production readers/replays."""
        event = original["event"]
        start = datetime.fromisoformat(event["timestamp"])
        initially_archived = event["action"] == "DOCUMENT_ARCHIVED"
        rows = []
        for number in range(1, count + 1):
            archived = initially_archived if number % 2 == 0 else not initially_archived
            action = "DOCUMENT_ARCHIVED" if archived else "DOCUMENT_ARCHIVE_RESTORED"
            operation_id = f"{event['operation_id']}-later-{number}"
            rows.append({
                "id": identity("document-archive-audit", event["profile_id"],
                               event["application_id"], action, operation_id),
                "entity_id": event["application_id"] or event["profile_id"],
                "action": action,
                "payload": {**event["payload"], "operation_id": operation_id},
                "timestamp": (start + timedelta(microseconds=number)).isoformat(),
            })
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert(), rows)

    def _snapshot(self):
        # Include all profiles' audit history and immutable revision metadata,
        # so a replay cannot hide an insertion, rewrite, or cross-profile write.
        result = {}
        with self.store.engine.connect() as connection:
            for table in (m.audit, m.documents, m.resume_revisions, m.uploaded_resumes):
                rows = [dict(row) for row in connection.execute(
                    select(table).order_by(table.c.id)
                ).mappings()]
                encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
                result[table.name] = (len(rows), hashlib.sha256(encoded).hexdigest())
        return result

    def test_bulk_state_keeps_older_documents_after_busy_sibling_history(self):
        for scope in ("uploaded", "generated"):
            with self.subTest(scope=scope):
                older, application_id = self._revision(scope, "older")
                busy, _ = self._revision(scope, "busy")
                self._archive(older, f"{scope}-archive-older")
                original = self._archive(busy, f"{scope}-archive-busy")
                self._later_transitions(original, 1001)
                before = self._snapshot()
                state = document_archive_state(
                    self.store, profile_id="fixture", application_id=application_id,
                )
                self.assertEqual(self._snapshot(), before)
                self.assertEqual(state, {older: True, busy: False})

    def test_old_archive_replay_preserves_restored_state_and_history(self):
        for scope in ("uploaded", "generated"):
            with self.subTest(scope=scope):
                document_id, application_id = self._revision(scope, "archive-replay")
                operation_id = f"{scope}-original-archive"
                original = self._archive(document_id, operation_id)
                self._later_transitions(original, 1001)
                before = self._snapshot()
                replay = self._archive(document_id, operation_id)
                self.assertEqual(self._snapshot(), before)
                self.assertEqual(replay["event"], original["event"])
                self.assertEqual((replay["archived"], replay["changed"]), (False, False))
                self.assertEqual(document_archive_metadata(
                    self.store, "fixture", application_id, document_id,
                ), {"archived": False, "archived_at": None})

    def test_old_restore_replay_preserves_archived_state_and_history(self):
        for scope in ("uploaded", "generated"):
            with self.subTest(scope=scope):
                document_id, _ = self._revision(scope, "restore-replay")
                self._archive(document_id, f"{scope}-initial-archive")
                operation_id = f"{scope}-original-restore"
                original = restore_document(
                    self.store, profile_id="fixture", document_id=document_id,
                    operation_id=operation_id,
                )
                self._later_transitions(original, 1001)
                before = self._snapshot()
                replay = restore_document(
                    self.store, profile_id="fixture", document_id=document_id,
                    operation_id=operation_id,
                )
                self.assertEqual(self._snapshot(), before)
                self.assertEqual(replay["event"], original["event"])
                self.assertEqual((replay["archived"], replay["changed"]), (True, False))

    def test_old_operation_conflict_is_rejected_even_when_target_is_already_archived(self):
        for scope in ("uploaded", "generated"):
            with self.subTest(scope=scope):
                first, _ = self._revision(scope, "first-owner")
                second, _ = self._revision(scope, "second-owner")
                operation_id = f"{scope}-claimed-operation"
                self._archive(first, operation_id)
                original = self._archive(second, f"{scope}-second-archive")
                self._later_transitions(original, 1000)
                before = self._snapshot()
                with self.assertRaisesRegex(ValueError, "reused"):
                    self._archive(second, operation_id)
                self.assertEqual(self._snapshot(), before)

    def test_history_reads_and_operation_ids_remain_profile_scoped(self):
        for scope in ("uploaded", "generated"):
            with self.subTest(scope=scope):
                owned, application_id = self._revision(scope, "isolated")
                other, other_application = self._revision(scope, "isolated", profile_id="other")
                operation_id = f"{scope}-same-request-key"
                first = self._archive(owned, operation_id)
                self._later_transitions(first, 1001)
                second = self._archive(other, operation_id, profile_id="other")
                self.assertNotEqual(first["event"]["id"], second["event"]["id"])
                before = self._snapshot()
                self.assertEqual(document_archive_state(
                    self.store, profile_id="other", application_id=other_application,
                ), {other: True})
                self.assertEqual(document_archive_state(
                    self.store, profile_id="fixture", application_id=application_id,
                ), {owned: False})
                for action in (archive_document, restore_document):
                    with self.assertRaises(LookupError):
                        action(self.store, profile_id="other", document_id=owned,
                               operation_id="cross-profile-attempt")
                if application_id is not None:
                    with self.assertRaises(LookupError):
                        document_archive_state(
                            self.store, profile_id="other", application_id=application_id,
                        )
                self.assertEqual(self._snapshot(), before)

    def test_equal_timestamps_keep_existing_event_identity_tie_order(self):
        for scope in ("uploaded", "generated"):
            with self.subTest(scope=scope):
                document_id, application_id = self._revision(scope, "timestamp-tie")
                original = self._archive(document_id, f"{scope}-tie-initial")["event"]
                timestamp = (datetime.fromisoformat(original["timestamp"])
                             + timedelta(seconds=1)).isoformat()
                rows = []
                for action in ("DOCUMENT_ARCHIVED", "DOCUMENT_ARCHIVE_RESTORED"):
                    operation_id = f"{scope}-tie-{action}"
                    rows.append({
                        "id": identity("document-archive-audit", "fixture", application_id,
                                       action, operation_id),
                        "entity_id": application_id or "fixture", "action": action,
                        "payload": {**original["payload"], "operation_id": operation_id},
                        "timestamp": timestamp,
                    })
                # Insert the smaller identity first: ordering by timestamp
                # alone must not silently select the first inserted tie.
                rows.sort(key=lambda row: row["id"])
                with self.store.engine.begin() as connection:
                    connection.execute(m.audit.insert(), rows)
                expected = rows[-1]["action"] == "DOCUMENT_ARCHIVED"
                before = self._snapshot()
                self.assertEqual(document_archive_state(
                    self.store, profile_id="fixture", application_id=application_id,
                ), {document_id: expected})
                self.assertEqual(document_archive_metadata(
                    self.store, "fixture", application_id, document_id,
                )["archived"], expected)
                self.assertEqual(self._snapshot(), before)

    def test_uploaded_and_generated_scopes_can_share_an_audit_entity_id(self):
        generated, application_id = self._revision("generated", "scope-collision")
        self.store.save_profile({
            "id": application_id, "name": "Synthetic colliding profile",
            "email": "collision@example.test", "facts": [], "answers": [],
        })
        uploaded, _ = self._revision("uploaded", "scope-collision", profile_id=application_id)
        self._archive(generated, "same-scope-collision-key")
        self._archive(uploaded, "same-scope-collision-key", profile_id=application_id)
        before = self._snapshot()
        self.assertEqual(document_archive_state(
            self.store, profile_id="fixture", application_id=application_id,
        ), {generated: True})
        self.assertEqual(document_archive_state(
            self.store, profile_id=application_id,
        ), {uploaded: True})
        self.assertEqual(self._snapshot(), before)

    def test_router_old_replay_reports_unchanged_current_state(self):
        app = FastAPI()
        app.include_router(create_document_archive_router(self.store))
        with TestClient(app) as client:
            for scope in ("uploaded", "generated"):
                with self.subTest(scope=scope):
                    document_id, _ = self._revision(scope, "router-replay")
                    operation_id = f"{scope}-router-original"
                    original = self._archive(document_id, operation_id)
                    self._later_transitions(original, 1001)
                    before = self._snapshot()
                    response = client.post(
                        f"/profiles/fixture/document-library/{document_id}/archive",
                        headers={"Idempotency-Key": operation_id},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self._snapshot(), before)
                    self.assertEqual(response.json(), {
                        "profile_id": "fixture", "document_id": document_id,
                        "archived": False, "archived_at": None, "changed": False,
                    })

    def test_supporting_selection_keeps_old_archived_upload_unavailable(self):
        application_id = self._application(name="supporting")
        older, _ = self._revision("uploaded", "older", document_kind="SUPPORTING_DOCUMENT")
        busy, _ = self._revision("uploaded", "busy", document_kind="SUPPORTING_DOCUMENT")
        active, _ = self._revision("uploaded", "active", document_kind="CERTIFICATION")
        self._archive(older, "supporting-archive-older")
        original = self._archive(busy, "supporting-archive-busy")
        self._later_transitions(original, 1001)
        before = self._snapshot()
        with self.subTest(action="read_available"):
            selection = list_selection(self.store, "fixture", application_id)
            self.assertEqual(self._snapshot(), before)
            self.assertEqual({row["revision_id"] for row in selection["available"]},
                             {busy, active})
        with self.subTest(action="select_archived"):
            with self.assertRaisesRegex(SupportingDocumentError, "SUPPORTING_DOCUMENT_UNAVAILABLE"):
                select_documents(self.store, "fixture", application_id, [older], "select-archived")
            self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
