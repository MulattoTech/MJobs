"""Workday document-selection boundary checks owned by T04 helper B."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
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


if __name__ == "__main__":
    unittest.main()
