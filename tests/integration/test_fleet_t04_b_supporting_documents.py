"""Workday document-selection boundary checks owned by T04 helper B."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity, now
from services.applications.supporting_documents import (
    SupportingDocumentError, read_selected_files, select_documents,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class FleetT04SupportingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t04-documents-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'documents.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as source_origin:
            discover(self.store, source_origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        with self.store.engine.begin() as connection:
            route_id = self.store.application(self.application_id)["route_id"]
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                provider="greenhouse", url="https://boards.greenhouse.io/example/jobs/12345",
            ))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            operation_mode="FIXTURE",
        ))
        self.prefix = f"/profiles/fixture/applications/{self.application_id}"
        self.selection_url = self.prefix + "/controlled-dry-run/supporting-documents"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _upload(self, content=b"Synthetic Workday supporting evidence"):
        return self.client.post(
            "/profiles/fixture/document-library", content=content,
            headers={
                "Content-Type": "text/plain", "X-Filename": "fixture-evidence.txt",
                "X-Document-Kind": "SUPPORTING_DOCUMENT",
                "X-Document-Label": "Fixture evidence",
            },
        )

    def test_controlled_fixture_selection_survives_lost_reply_and_store_reopen(self):
        revision_id = self._upload().json()["revision_id"]
        payload = {"revision_ids": [revision_id], "request_id": "t04-lost-reply"}

        # Simulate a committed request whose response was lost, then retry at the public route.
        first = select_documents(
            self.store, "fixture", self.application_id,
            payload["revision_ids"], payload["request_id"], _controlled_fixture=True,
        )
        self.assertFalse(first["replayed"])
        replay = self.client.put(self.selection_url, json=payload)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["selected_revision_ids"], [revision_id])
        self.assertEqual(self.client.get(self.selection_url).json()["selected_revision_ids"], [revision_id])
        selected = read_selected_files(
            self.store, self.root, "fixture", self.application_id, _controlled_fixture=True,
        )
        self.assertEqual(selected[0]["content"], b"Synthetic Workday supporting evidence")

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            operation_mode="FIXTURE",
        ))
        reread = self.client.get(self.selection_url)
        self.assertEqual(reread.status_code, 200, reread.text)
        self.assertEqual(reread.json()["selected_revision_ids"], [revision_id])
        self.assertEqual(read_selected_files(
            self.store, self.root, "fixture", self.application_id, _controlled_fixture=True,
        )[0]["content"], b"Synthetic Workday supporting evidence")

        conflict = self.client.put(self.selection_url, json={
            "revision_ids": [], "request_id": payload["request_id"],
        })
        self.assertEqual(conflict.status_code, 409, conflict.text)

        archived = self.client.post(
            f"/profiles/fixture/document-library/{revision_id}/archive",
            headers={"Idempotency-Key": "t04-archive-selected"},
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        with self.assertRaisesRegex(SupportingDocumentError, "SUPPORTING_DOCUMENT_UNAVAILABLE"):
            read_selected_files(
                self.store, self.root, "fixture", self.application_id,
                _controlled_fixture=True,
            )
        preview = self.client.get(self.prefix + "/controlled-dry-run")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("SUPPORTING_DOCUMENT_UNAVAILABLE", preview.json()["blockers"])
        self.assertFalse(preview.json()["ready"])

    def test_fixture_selection_requires_a_bound_fixture_workspace(self):
        other_database = f"sqlite:///{(self.root / 'personal.db').as_posix()}"
        personal_store = Store(other_database)
        personal_store.migrate()
        personal_store.bind_workspace_mode("PERSONAL")
        try:
            for operation in (
                lambda: select_documents(personal_store, "fixture", "missing", [], "blocked",
                                         _controlled_fixture=True),
                lambda: read_selected_files(personal_store, self.root, "fixture", "missing",
                                            _controlled_fixture=True),
            ):
                with self.subTest(operation=operation), self.assertRaises(SupportingDocumentError) as error:
                    operation()
                self.assertEqual(error.exception.code, "CONTROLLED_FIXTURE_WORKSPACE_REQUIRED")
            personal_client = TestClient(create_app(
                personal_store, "http://127.0.0.1:1", self.root,
                operation_mode="PERSONAL",
            ))
            try:
                response = personal_client.put(
                    "/profiles/fixture/applications/missing/controlled-dry-run/supporting-documents",
                    json={"revision_ids": [], "request_id": "blocked"},
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"],
                                 "CONTROLLED_FIXTURE_WORKSPACE_REQUIRED")
            finally:
                personal_client.close()
        finally:
            personal_store.engine.dispose()

    def test_selected_bytes_use_one_event_when_a_new_selection_arrives_mid_read(self):
        first_content = b"First immutable Workday selection"
        first_revision = self._upload(first_content).json()["revision_id"]
        second_content = b"Second Workday selection"
        second_revision = self._upload(second_content).json()["revision_id"]
        first = select_documents(
            self.store, "fixture", self.application_id, [first_revision], "t04-race-first",
            _controlled_fixture=True,
        )
        self.assertEqual(first["selected_revision_ids"], [first_revision])

        from services.applications import supporting_documents as service
        original_events = service._selection_events
        swapped = False

        def events_then_new_selection(connection, application_id, *, _controlled_fixture=False):
            nonlocal swapped
            events = original_events(connection, application_id,
                                     _controlled_fixture=_controlled_fixture)
            if not swapped:
                swapped = True
                second = connection.execute(select(
                    m.resume_revisions.c.upload_id,
                    m.resume_revisions.c.document_kind,
                    m.uploaded_resumes.c.sha256,
                ).join(m.uploaded_resumes,
                       m.uploaded_resumes.c.id == m.resume_revisions.c.upload_id).where(
                    m.resume_revisions.c.id == second_revision,
                )).mappings().one()
                payload = {
                    "profile_id": "fixture", "application_id": self.application_id,
                    "job_id": self.job_id,
                    "items": [{
                        "revision_id": second_revision,
                        "upload_id": second["upload_id"],
                        "document_kind": second["document_kind"],
                        "sha256": second["sha256"],
                        "size_bytes": len(second_content),
                    }],
                    "context": "CONTROLLED_FIXTURE", "request_id": "t04-race-second",
                    "sequence": max((item["payload"].get("sequence", 0) for item in events), default=0) + 1,
                }
                connection.execute(m.audit.insert().values(
                    id=identity("application-fixture-supporting-documents",
                                self.application_id, "t04-race-second"),
                    entity_id=self.application_id,
                    action="APPLICATION_FIXTURE_SUPPORTING_DOCUMENTS_SELECTED",
                    payload=payload, timestamp=now(),
                ))
                connection.commit()
            return events

        with patch.object(service, "_selection_events", events_then_new_selection):
            selected = read_selected_files(
                self.store, self.root, "fixture", self.application_id,
                _controlled_fixture=True,
            )
        self.assertTrue(swapped)
        self.assertEqual([item["revision_id"] for item in selected], [first_revision])
        self.assertEqual(selected[0]["content"], first_content)

    def test_new_selection_records_size_and_changed_size_metadata_fails_closed(self):
        content = b"Size-bound synthetic Workday document"
        revision_id = self._upload(content).json()["revision_id"]
        self.client.put(self.selection_url, json={
            "revision_ids": [revision_id], "request_id": "t04-size-metadata",
        })
        with self.store.engine.connect() as connection:
            event = connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "APPLICATION_FIXTURE_SUPPORTING_DOCUMENTS_SELECTED",
            )).mappings().one()
        original_payload = event["payload"]
        self.assertEqual(original_payload["items"][0].get("size_bytes"), len(content))

        for size_bytes in (len(content) + 1, "malformed", None):
            changed = {**original_payload, "items": [
                {**original_payload["items"][0], "size_bytes": size_bytes},
            ]}
            with self.store.engine.begin() as connection:
                connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(
                    payload=changed,
                ))
            with self.subTest(size_bytes=size_bytes), self.assertRaisesRegex(
                    SupportingDocumentError, "SUPPORTING_DOCUMENT_CHANGED"):
                read_selected_files(
                    self.store, self.root, "fixture", self.application_id,
                    _controlled_fixture=True,
                )

        malformed = {**original_payload, "items": [None]}
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(
                payload=malformed,
            ))
        with self.assertRaisesRegex(SupportingDocumentError, "SUPPORTING_DOCUMENT_CHANGED"):
            read_selected_files(
                self.store, self.root, "fixture", self.application_id,
                _controlled_fixture=True,
            )

        legacy = {**original_payload, "items": [
            {key: value for key, value in original_payload["items"][0].items()
             if key != "size_bytes"},
        ]}
        with self.store.engine.begin() as connection:
            connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(
                payload=legacy,
            ))
        self.assertEqual(read_selected_files(
            self.store, self.root, "fixture", self.application_id,
            _controlled_fixture=True,
        )[0]["content"], content)
        replay = self.client.put(self.selection_url, json={
            "revision_ids": [revision_id], "request_id": "t04-size-metadata",
        })
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])

        from services.applications import supporting_documents as service
        expected_record = {"sha256": original_payload["items"][0]["sha256"],
                           "size_bytes": len(content), "filename": "fixture-evidence.txt",
                           "media_type": "text/plain"}
        for record, returned_content in (
            ({**expected_record, "size_bytes": len(content) + 1}, content),
            (expected_record, content[:-1]),
        ):
            with patch.object(service, "read_resume", return_value=(record, returned_content)):
                with self.assertRaisesRegex(SupportingDocumentError, "SUPPORTING_DOCUMENT_CHANGED"):
                    read_selected_files(
                        self.store, self.root, "fixture", self.application_id,
                        _controlled_fixture=True,
                    )

    def test_workday_route_can_select_and_read_documents_without_preflight_hook(self):
        with self.store.engine.begin() as connection:
            route_id = self.store.application(self.application_id)["route_id"]
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                provider="workday",
                url="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R12345",
            ))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
        revision_id = self._upload(b"Synthetic Workday selection-only document").json()["revision_id"]
        payload = {
            "revision_ids": [revision_id], "request_id": "t04-workday-selection-only",
        }
        ordinary_url = self.prefix + "/supporting-documents"
        denied = self.client.put(ordinary_url, json=payload)
        self.assertEqual(denied.status_code, 422, denied.text)
        self.assertEqual(denied.json()["detail"]["code"],
                         "EXTERNAL_APPLICATION_DOCUMENTS_DISABLED")

        saved = self.client.put(self.selection_url, json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["selected_revision_ids"], [revision_id])
        self.assertEqual(self.client.get(self.selection_url).json()["selected_revision_ids"],
                         [revision_id])
        self.assertEqual(self.client.get(ordinary_url).json()["selected_revision_ids"], [])
        self.assertEqual(read_selected_files(
            self.store, self.root, "fixture", self.application_id,
            _controlled_fixture=True,
        )[0]["content"], b"Synthetic Workday selection-only document")


if __name__ == "__main__":
    unittest.main()
