"""Application answers stay private through creation, crashes and recovery."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, identity
from services.discovery.pipeline import discover
from services.documents import answers
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


QUESTION = "What should the synthetic reviewer know?"
ANSWER = "Synthetic private application answer."
CONTENT = f"Question: {QUESTION}\nAnswer: {ANSWER}\n".encode("utf-8")


class AnswerExportPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-answer-privacy-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'workspace.db').as_posix()}"
        self.store = Store(self.url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.application_id = prepare(
            self.store, self.store.jobs()[0]["id"], "fixture", self.root)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root),
            raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.endpoint = f"/profiles/fixture/applications/{self.application_id}/answers/export"
        self.payload = {"profile_id": "fixture", "items": [{
            "question": QUESTION, "text": ANSWER, "reviewed": True,
        }]}
        document_id = identity(self.application_id, "application-answer-export", 1)
        digest = hashlib.sha256(CONTENT).hexdigest()
        self.path = (self.root / self.application_id / "answers" / "1"
                     / f"{document_id}-{digest}.txt")

    def export(self):
        return self.client.post(self.endpoint, json=self.payload)

    def rows(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.application_id,
                m.documents.c.kind == "application-answer-export")).mappings().all()

    def seed_interrupted_file(self, content=CONTENT):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(content)
        if os.name == "posix":
            self.path.chmod(0o666)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_temporary_and_published_bytes_are_private_under_permissive_umask(self):
        observed = []
        original_fsync = os.fsync

        def observe(descriptor):
            observed.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
            return original_fsync(descriptor)

        previous_umask = os.umask(0)
        try:
            with patch.object(answers.os, "fsync", side_effect=observe):
                response = self.export()
        finally:
            os.umask(previous_umask)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(observed, "The actual export must fsync before publication")
        self.assertEqual(set(observed), {0o600})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.read_bytes(), CONTENT)
        self.assertEqual(len(self.rows()), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_process_death_leaves_private_bytes_and_retry_remains_safe(self):
        script = """
import os, sys
from types import SimpleNamespace
from unittest.mock import patch
from packages.persistence.store import Store
from services.documents import answers
store = Store(sys.argv[1])
original = os.fsync
def crash(descriptor):
    original(descriptor)
    os._exit(73)
os.umask(0)
with patch.object(answers.os, "fsync", side_effect=crash):
    answers.export_answers(store, sys.argv[2], sys.argv[3], [
        SimpleNamespace(question=sys.argv[4], text=sys.argv[5], answer_id=None)])
"""
        result = subprocess.run(
            [sys.executable, "-c", script, self.url, self.application_id,
             str(self.root), QUESTION, ANSWER],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 73, result.stderr.decode("utf-8", "replace"))
        temporary = list(self.path.parent.glob(".*.tmp"))
        self.assertEqual(len(temporary), 1)
        self.assertEqual(temporary[0].read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(temporary[0].stat().st_mode), 0o600)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.rows(), [])
        response = self.export()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.path.read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(temporary[0].read_bytes(), CONTENT)
        self.assertEqual(len(self.rows()), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_recovery_restricts_matching_bytes_before_recording_document(self):
        self.seed_interrupted_file()
        before_inode = self.path.stat().st_ino
        observed = []
        original_upsert = answers.upsert

        def observe(*args, **kwargs):
            observed.append(stat.S_IMODE(self.path.stat().st_mode))
            return original_upsert(*args, **kwargs)

        with patch.object(answers, "upsert", side_effect=observe):
            response = self.export()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(observed, [0o600])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.stat().st_ino, before_inode)
        self.assertEqual(self.path.read_bytes(), CONTENT)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_permission_failure_does_not_commit_an_export(self):
        self.seed_interrupted_file()
        with patch.object(answers.os, "fchmod", side_effect=PermissionError(
                "synthetic-private-storage-location")):
            response = self.export()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("synthetic-private-storage-location", response.text)
        self.assertEqual(response.json()["detail"], "ANSWER_EXPORT_STORAGE_UNAVAILABLE")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.path.read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o666)

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics required")
    def test_recovery_sync_failure_does_not_record_unconfirmed_permissions(self):
        self.seed_interrupted_file()
        with patch.object(answers.os, "fsync", side_effect=OSError(
                "synthetic-private-storage-location")):
            response = self.export()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "ANSWER_EXPORT_STORAGE_UNAVAILABLE")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.path.read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_corrupt_recovery_file_is_not_reused_or_changed(self):
        self.seed_interrupted_file(b"synthetic conflicting bytes")
        before_mode = stat.S_IMODE(self.path.stat().st_mode)
        response = self.export()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.path.read_bytes(), b"synthetic conflicting bytes")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), before_mode)

    @unittest.skipUnless(os.name == "posix", "POSIX hard-link semantics required")
    def test_hardlinked_recovery_file_cannot_change_an_unrelated_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        unrelated = self.root / "unrelated-synthetic-answer.txt"
        unrelated.write_bytes(CONTENT)
        unrelated.chmod(0o666)
        os.link(unrelated, self.path)
        response = self.export()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "ANSWER_EXPORT_PATH_INVALID")
        self.assertEqual(self.rows(), [])
        self.assertEqual(unrelated.read_bytes(), CONTENT)
        self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o666)
        self.assertEqual(unrelated.stat().st_nlink, 2)

    def test_failed_flush_removes_temporary_without_publishing_or_committing(self):
        with patch.object(answers.os, "fsync", side_effect=OSError(
                "synthetic-private-storage-location")):
            response = self.export()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn("synthetic-private-storage-location", response.text)
        self.assertEqual(response.json()["detail"], "ANSWER_EXPORT_STORAGE_UNAVAILABLE")
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])

    def test_reopened_download_preserves_scope_hash_and_cache_policy(self):
        response = self.export()
        self.assertEqual(response.status_code, 201, response.text)
        document = self.rows()[0]
        self.assertEqual(document["sha256"]["txt"], hashlib.sha256(CONTENT).hexdigest())
        self.store.save_profile(dict(PROFILE, id="other", name="Other fixture",
                                     email="other@example.test"))
        self.client.close()
        self.store.engine.dispose()
        reopened = Store(self.url)
        self.addCleanup(reopened.engine.dispose)
        with TestClient(create_app(reopened, "http://127.0.0.1:1", self.root)) as client:
            download = f"/profiles/fixture/documents/{document['id']}/txt"
            owned = client.get(download)
            self.assertEqual(owned.status_code, 200, owned.text)
            self.assertEqual(owned.content, CONTENT)
            self.assertEqual(owned.headers.get("cache-control"), "no-store")
            denied = client.get(download.replace("/profiles/fixture/", "/profiles/other/"))
            self.assertEqual(denied.status_code, 404)
            self.assertNotIn(ANSWER, denied.text)
