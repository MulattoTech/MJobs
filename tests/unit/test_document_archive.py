"""Document archive state is durable without changing immutable revisions."""
from pathlib import Path
import hashlib
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.documents.archive import (
    archive_document,
    document_archive_state,
    restore_document,
)
from services.documents.generate import prepare
from fixtures.mock_server import fixture_server
from tests.integration.test_spine import PROFILE


class DocumentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-document-archive-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "archive.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile(dict(PROFILE, id="other", email="other@example.test"))
        with fixture_server() as origin:
            from services.discovery.pipeline import discover
            discover(self.store, origin, "fixture")
        job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        with self.store.engine.connect() as connection:
            self.generated = dict(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "resume",
            )).mappings().one())
        self.generated_paths = {
            name: self.root / self.application_id / relative
            for name, relative in self.generated["files"].items()
        }
        self.generated_bytes = {name: path.read_bytes()
                                for name, path in self.generated_paths.items()}

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def test_generated_revision_archive_restore_is_idempotent_and_preserves_bytes(self):
        document_id = self.generated["id"]
        archived = archive_document(self.store, profile_id="fixture",
                                    document_id=document_id, operation_id="archive-1")
        replay = archive_document(self.store, profile_id="fixture",
                                  document_id=document_id, operation_id="archive-1")
        self.assertEqual(archived["event"]["id"], replay["event"]["id"])
        self.assertTrue(document_archive_state(
            self.store, profile_id="fixture", application_id=self.application_id
        )[document_id])
        restored = restore_document(self.store, profile_id="fixture",
                                    document_id=document_id, operation_id="restore-1")
        self.assertFalse(restored["archived"])
        self.assertFalse(document_archive_state(
            self.store, profile_id="fixture", application_id=self.application_id
        )[document_id])
        with self.store.engine.connect() as connection:
            row = dict(connection.execute(select(m.documents).where(
                m.documents.c.id == document_id)).mappings().one())
            events = list(connection.execute(select(m.audit).where(
                m.audit.c.action.in_(("DOCUMENT_ARCHIVED", "DOCUMENT_ARCHIVE_RESTORED")),
                m.audit.c.payload["document_id"].as_string() == document_id,
            )).mappings())
        self.assertEqual(row, self.generated)
        self.assertEqual([event["action"] for event in events],
                         ["DOCUMENT_ARCHIVED", "DOCUMENT_ARCHIVE_RESTORED"])
        for name, path in self.generated_paths.items():
            self.assertEqual(path.read_bytes(), self.generated_bytes[name])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             self.generated["sha256"][name])

    def test_generated_cover_letter_can_be_archived_and_restored(self):
        with self.store.engine.connect() as connection:
            letter = dict(connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "cover-letter",
            )).mappings().one())
        paths = {name: self.root / self.application_id / relative
                 for name, relative in letter["files"].items()}
        original = {name: path.read_bytes() for name, path in paths.items()}

        archived = archive_document(self.store, profile_id="fixture",
                                    document_id=letter["id"], operation_id="archive-letter")
        self.assertTrue(archived["archived"])
        self.assertTrue(document_archive_state(
            self.store, profile_id="fixture", application_id=self.application_id
        )[letter["id"]])
        restored = restore_document(self.store, profile_id="fixture",
                                    document_id=letter["id"], operation_id="restore-letter")
        self.assertFalse(restored["archived"])
        for name, path in paths.items():
            self.assertEqual(path.read_bytes(), original[name])

    def test_profile_only_uploaded_revision_is_archived_without_changing_original_bytes(self):
        revision_id = "uploaded-revision-archive-test"
        upload_id = "uploaded-document-archive-test"
        content = b"synthetic private resume bytes"
        digest = hashlib.sha256(content).hexdigest()
        (self.root / "uploads").mkdir(exist_ok=True)
        (self.root / "uploads" / f"{upload_id}.txt").write_bytes(content)
        with self.store.engine.begin() as connection:
            connection.execute(m.uploaded_resumes.insert().values(
                id=upload_id, profile_id="fixture", filename="resume.txt", format="txt",
                media_type="text/plain", size_bytes=len(content), sha256=digest,
                status="STORED_UNREVIEWED", created_at="2026-10-08T00:00:00+00:00"))
            connection.execute(m.resume_revisions.insert().values(
                id=revision_id, profile_id="fixture", upload_id=upload_id,
                series_id="series-1", label="Synthetic resume", version=1,
                created_at="2026-10-08T00:00:00+00:00", document_kind="BASE_RESUME"))

        before = self.store.count("audit_events")
        with self.assertRaises(LookupError):
            archive_document(self.store, profile_id="other", document_id=revision_id,
                             operation_id="cross-profile-upload-archive")
        self.assertEqual(self.store.count("audit_events"), before)
        result = archive_document(self.store, profile_id="fixture",
                                  document_id=revision_id, operation_id="archive-upload-1")
        self.assertTrue(result["archived"])
        self.assertTrue(document_archive_state(self.store, profile_id="fixture")[revision_id])
        self.assertEqual((self.root / "uploads" / f"{upload_id}.txt").read_bytes(), content)
        restored = restore_document(self.store, profile_id="fixture",
                                    document_id=revision_id, operation_id="restore-upload-1")
        self.assertFalse(restored["archived"])
        self.assertFalse(document_archive_state(self.store, profile_id="fixture")[revision_id])
        self.assertEqual((self.root / "uploads" / f"{upload_id}.txt").read_bytes(), content)
        self.assertEqual(self.store.count("resume_revisions"), 1)

    def test_cross_profile_archive_and_restore_are_denied_without_audit_changes(self):
        document_id = self.generated["id"]
        with self.assertRaises(LookupError):
            archive_document(self.store, profile_id="other", document_id=document_id,
                             operation_id="cross-profile-archive")
        archive_document(self.store, profile_id="fixture", document_id=document_id,
                         operation_id="archive-owned")
        before = self.store.count("audit_events")
        with self.assertRaises(LookupError):
            restore_document(self.store, profile_id="other", document_id=document_id,
                             operation_id="cross-profile-restore")
        self.assertEqual(self.store.count("audit_events"), before)
        self.assertTrue(document_archive_state(
            self.store, profile_id="fixture", application_id=self.application_id
        )[document_id])
        with self.assertRaises(LookupError):
            document_archive_state(self.store, profile_id="other",
                                   application_id=self.application_id)


if __name__ == "__main__":
    unittest.main()
