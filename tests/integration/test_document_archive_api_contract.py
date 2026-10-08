"""Router contract for profile-owned immutable document archive actions."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.document_archive import (
    create_document_archive_router,
    document_archive_metadata,
)
from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE


class DocumentArchiveApiContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-document-archive-api-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output_dir = self.root / "outputs"
        self.store = Store(f"sqlite:///{(self.root / 'archive-api.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(deepcopy(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "email": "other@example.test"})
        self.first_bytes = b"Synthetic private resume revision one.\n"
        self.second_bytes = b"Synthetic private resume revision two.\n"
        self.first = save_resume(
            self.store, self.output_dir, "fixture", "synthetic-one.txt", "text/plain",
            self.first_bytes,
        )
        self.second = save_resume(
            self.store, self.output_dir, "fixture", "synthetic-two.txt", "text/plain",
            self.second_bytes, series_id=self.first["series_id"],
        )
        app = create_app(
            self.store, "http://127.0.0.1:1", self.output_dir,
            "redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )
        # Register the factory directly as the contract under test. On a branch
        # where main.py already mounts it, the duplicate routes have the same
        # handlers/store and preserve compatibility across FastAPI router APIs.
        app.include_router(create_document_archive_router(self.store))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _download(self, document_id):
        response = self.client.get(f"/profiles/fixture/document-library/{document_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.content

    def test_archive_restore_idempotency_ownership_metadata_and_bytes(self):
        document_id = self.first["revision_id"]
        other_document_id = self.second["revision_id"]
        archive_url = f"/profiles/fixture/document-library/{document_id}/archive"
        restore_url = f"/profiles/fixture/document-library/{document_id}/restore"
        before_download = self._download(document_id)
        with self.store.engine.connect() as connection:
            revision_before = dict(connection.execute(select(m.resume_revisions).where(
                m.resume_revisions.c.id == document_id,
            )).mappings().one())
            upload_before = dict(connection.execute(select(m.uploaded_resumes).where(
                m.uploaded_resumes.c.id == self.first["id"],
            )).mappings().one())
        self.assertEqual(before_download, self.first_bytes)
        self.assertEqual(hashlib.sha256(before_download).hexdigest(), upload_before["sha256"])

        missing_key = self.client.post(archive_url)
        self.assertEqual(missing_key.status_code, 422)
        malformed_key = self.client.post(archive_url, headers={"Idempotency-Key": "bad key"})
        self.assertEqual(malformed_key.status_code, 400)

        before_events = self.store.count("audit_events")
        denied = self.client.post(
            f"/profiles/other/document-library/{document_id}/archive",
            headers={"Idempotency-Key": "other-profile-attempt"},
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(self.store.count("audit_events"), before_events)

        archived = self.client.post(archive_url, headers={"Idempotency-Key": "archive-revision-1"})
        self.assertEqual(archived.status_code, 200, archived.text)
        archived_body = archived.json()
        self.assertEqual(set(archived_body), {"profile_id", "document_id", "archived", "archived_at", "changed"})
        self.assertEqual(archived_body["profile_id"], "fixture")
        self.assertEqual(archived_body["document_id"], document_id)
        self.assertTrue(archived_body["archived"])
        self.assertTrue(archived_body["changed"])
        self.assertTrue(archived_body["archived_at"])
        serialized = repr(archived_body)
        self.assertNotIn("operation_id", serialized)
        self.assertNotIn("payload", serialized)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn(self.first_bytes.decode(), serialized)

        replay = self.client.post(archive_url, headers={"Idempotency-Key": "archive-revision-1"})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["changed"], False)
        self.assertEqual(replay.json()["archived_at"], archived_body["archived_at"])
        metadata = document_archive_metadata(self.store, "fixture", None, document_id)
        self.assertEqual(metadata, {"archived": True, "archived_at": archived_body["archived_at"]})

        events_before_conflicts = self.store.count("audit_events")
        reused_for_another_document = self.client.post(
            f"/profiles/fixture/document-library/{other_document_id}/archive",
            headers={"Idempotency-Key": "archive-revision-1"},
        )
        reused_for_other_action = self.client.post(
            restore_url, headers={"Idempotency-Key": "archive-revision-1"},
        )
        self.assertEqual(reused_for_another_document.status_code, 409)
        self.assertEqual(reused_for_other_action.status_code, 409)
        self.assertEqual(self.store.count("audit_events"), events_before_conflicts)

        restored = self.client.post(restore_url, headers={"Idempotency-Key": "restore-revision-1"})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(set(restored.json()), {"profile_id", "document_id", "archived", "archived_at", "changed"})
        self.assertEqual(restored.json()["profile_id"], "fixture")
        self.assertEqual(restored.json()["document_id"], document_id)
        self.assertFalse(restored.json()["archived"])
        self.assertIsNone(restored.json()["archived_at"])
        self.assertTrue(restored.json()["changed"])
        self.assertEqual(document_archive_metadata(self.store, "fixture", None, document_id),
                         {"archived": False, "archived_at": None})

        after_download = self._download(document_id)
        with self.store.engine.connect() as connection:
            revision_after = dict(connection.execute(select(m.resume_revisions).where(
                m.resume_revisions.c.id == document_id,
            )).mappings().one())
            upload_after = dict(connection.execute(select(m.uploaded_resumes).where(
                m.uploaded_resumes.c.id == self.first["id"],
            )).mappings().one())
        self.assertEqual(after_download, before_download)
        self.assertEqual(hashlib.sha256(after_download).hexdigest(), upload_after["sha256"])
        self.assertEqual(revision_after, revision_before)
        self.assertEqual(upload_after, upload_before)


if __name__ == "__main__":
    unittest.main()
