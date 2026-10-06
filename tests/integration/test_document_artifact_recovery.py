"""Crash recovery coverage for immutable generated document revisions."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.artifacts import (
    list_quarantined,
    purge_expired_quarantine,
    quarantine_artifact,
    restore_quarantined,
    scan_artifacts,
)
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE
from fixtures.mock_server import fixture_server


class GeneratedDocumentCrashTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-artifact-crash-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'trial.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_crash_during_revision_write_preserves_previous_revision_and_hides_partial_next(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = self.store.jobs()[0]
            application_id = prepare(self.store, job["id"], "fixture", self.root)
            previous = application_documents(self.store, application_id)
            previous_history = copy.deepcopy(previous)
            previous_bytes = {
                relative: (self.root / application_id / relative).read_bytes()
                for document in previous for relative in document["files"].values()
            }
            profile_version = self.store.application(application_id)["profile_version"]
            next_revision = self.root / application_id / profile_version / "2"
            script = r"""
import os
from pathlib import Path
import docx.document
from packages.persistence.store import Store
from services.documents.generate import prepare

def interrupt(self, path, *args, **kwargs):
    Path(path).write_bytes(b"partial docx after abrupt worker exit")
    os._exit(73)

docx.document.Document.save = interrupt
store = Store(os.environ["MJOBS_TEST_DATABASE_URL"])
prepare(store, os.environ["MJOBS_TEST_JOB_ID"], "fixture",
        os.environ["MJOBS_TEST_OUTPUT_DIR"], cover_letter_length="concise")
"""
            env = dict(os.environ)
            env.update({
                "MJOBS_TEST_DATABASE_URL": str(self.store.engine.url),
                "MJOBS_TEST_JOB_ID": job["id"],
                "MJOBS_TEST_OUTPUT_DIR": str(self.root),
            })
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, timeout=20, check=False,
            )

            self.assertEqual(result.returncode, 73, result.stderr.decode("utf-8", "replace"))
            for relative, payload in previous_bytes.items():
                self.assertEqual((self.root / application_id / relative).read_bytes(), payload)
            self.assertFalse(next_revision.exists(), "partial revision became visible at its final path")
            self.assertEqual(application_documents(self.store, application_id), previous_history)

    def _old_orphan(self, name: str, *, now: datetime) -> Path:
        revision = self.root / f"orphan-{name}" / "profile-v1" / "9"
        revision.mkdir(parents=True)
        (revision / "resume.txt").write_bytes(f"synthetic orphan {name}".encode())
        old = now - timedelta(days=3)
        timestamp = old.timestamp()
        os.utime(revision / "resume.txt", (timestamp, timestamp))
        os.utime(revision, (timestamp, timestamp))
        return revision

    def test_scan_protects_referenced_uploads_and_unknown_or_debug_files(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = self.store.jobs()[0]
            application_id = prepare(self.store, job["id"], "fixture", self.root)
            save_resume(self.store, self.root, "fixture", "original.txt", "text/plain", b"private synthetic resume")
            (self.root / "debug").mkdir()
            (self.root / "debug" / "capture.html").write_text("synthetic debug", encoding="utf-8")
            (self.root / "operator-note.txt").write_text("preserve unknown local file", encoding="utf-8")
            mixed_revision = self._old_orphan("mixed", now=datetime.now(timezone.utc))
            (mixed_revision / "operator-note.md").write_text(
                "preserve unknown file beside generated output", encoding="utf-8")
            old = datetime.now(timezone.utc) - timedelta(days=3)
            staging = self.root / "stale" / "profile-v2" / ".2.staging-crashed"
            staging.mkdir(parents=True)
            staged_temp = staging / ".resume.docx.worker.tmp"
            staged_temp.write_bytes(b"partial staged document")
            temp_file = self.root / "stale" / "profile-v3" / "7" / ".cover-letter.pdf.worker.tmp"
            temp_file.parent.mkdir(parents=True)
            temp_file.write_bytes(b"partial generated file")
            for path in (staged_temp, staging, temp_file.parent, temp_file):
                os.utime(path, (old.timestamp(), old.timestamp()))
            now = datetime.now(timezone.utc)

            inventory = scan_artifacts(self.store, self.root, now=now)
            by_path = {item["path"]: item for item in inventory}
            documents = application_documents(self.store, application_id)
            for document in documents:
                for relative in document["files"].values():
                    path = f"{application_id}/{relative}"
                    self.assertEqual(by_path[path]["classification"], "REFERENCED_ARTIFACT")
                    self.assertFalse(by_path[path]["eligible"])
            upload = next(row for row in inventory if row["path"].startswith("uploads/")
                          and row["path"].endswith(".txt"))
            self.assertEqual(upload["classification"], "REFERENCED_ARTIFACT")
            self.assertFalse(upload["eligible"])
            self.assertEqual(by_path["debug/capture.html"]["classification"], "DEBUG_ARTIFACT_PROTECTED")
            self.assertFalse(by_path["debug/capture.html"]["eligible"])
            self.assertEqual(by_path["operator-note.txt"]["classification"], "UNCLASSIFIED_PROTECTED")
            self.assertFalse(by_path["operator-note.txt"]["eligible"])
            mixed_entry = by_path[mixed_revision.relative_to(self.root).as_posix()]
            self.assertEqual(mixed_entry["classification"], "UNCLASSIFIED_PROTECTED")
            self.assertFalse(mixed_entry["eligible"])
            staging_entry = by_path[staging.relative_to(self.root).as_posix()]
            self.assertEqual(staging_entry["classification"], "FAILED_GENERATION_STAGING")
            self.assertTrue(staging_entry["eligible"])
            temp_entry = by_path[temp_file.relative_to(self.root).as_posix()]
            self.assertEqual(temp_entry["classification"], "INCOMPLETE_TEMPORARY_FILE")
            self.assertTrue(temp_entry["eligible"])

    def test_quarantine_restore_collision_and_retention_purge_are_audited(self):
        now = datetime.now(timezone.utc)
        orphan = self._old_orphan("restore", now=now)
        original = orphan / "resume.txt"
        original_bytes = original.read_bytes()
        candidate = next(item for item in scan_artifacts(self.store, self.root, now=now)
                         if item.get("path") == orphan.relative_to(self.root).as_posix())
        self.assertTrue(candidate["eligible"])

        record = quarantine_artifact(self.store, self.root, candidate["artifact_id"], now=now)
        quarantine_id = record["quarantine_id"]
        self.assertFalse(orphan.exists())
        self.assertEqual(list_quarantined(self.root)[0]["state"], "QUARANTINED")
        payload = self.root / ".quarantine" / quarantine_id / "artifact" / "resume.txt"
        self.assertEqual(payload.read_bytes(), original_bytes)
        with self.store.engine.connect() as connection:
            actions = {row["action"] for row in connection.execute(select(m.audit).where(
                m.audit.c.entity_id == quarantine_id)).mappings()}
        self.assertIn("ARTIFACT_QUARANTINED", actions)

        original.parent.mkdir(parents=True)
        original.write_bytes(b"newer destination must never be overwritten")
        with self.assertRaises(FileExistsError):
            restore_quarantined(self.store, self.root, quarantine_id, now=now)
        self.assertEqual(payload.read_bytes(), original_bytes)
        original.unlink()
        restored = restore_quarantined(self.store, self.root, quarantine_id, now=now)
        self.assertEqual(restored["state"], "RESTORED")
        self.assertEqual(original.read_bytes(), original_bytes)

        purge_orphan = self._old_orphan("purge", now=now)
        purge_candidate = next(item for item in scan_artifacts(self.store, self.root, now=now)
                               if item.get("path") == purge_orphan.relative_to(self.root).as_posix())
        purge_record = quarantine_artifact(self.store, self.root, purge_candidate["artifact_id"], now=now)
        later = now + timedelta(days=31)
        self.assertEqual(purge_expired_quarantine(self.store, self.root, now=later),
                         [purge_record["quarantine_id"]])
        self.assertFalse((self.root / ".quarantine" / purge_record["quarantine_id"]).exists())
        purged_manifest = next(item for item in list_quarantined(self.root)
                               if item["quarantine_id"] == purge_record["quarantine_id"])
        self.assertEqual(purged_manifest["state"], "PURGED")
        with self.store.engine.connect() as connection:
            actions = {row["action"] for row in connection.execute(select(m.audit).where(
                m.audit.c.entity_id == purge_record["quarantine_id"])).mappings()}
        self.assertIn("ARTIFACT_PURGED", actions)

    def test_corrupted_quarantine_payload_is_neither_restored_nor_purged(self):
        now = datetime.now(timezone.utc)
        orphan = self._old_orphan("tamper", now=now)
        candidate = next(item for item in scan_artifacts(self.store, self.root, now=now)
                         if item.get("path") == orphan.relative_to(self.root).as_posix())
        record = quarantine_artifact(self.store, self.root, candidate["artifact_id"], now=now)
        payload = self.root / ".quarantine" / record["quarantine_id"] / "artifact" / "resume.txt"
        payload.write_bytes(b"tampered")

        with self.assertRaisesRegex(ValueError, "integrity"):
            restore_quarantined(self.store, self.root, record["quarantine_id"], now=now)
        self.assertEqual(purge_expired_quarantine(
            self.store, self.root, now=now + timedelta(days=31)), [])
        self.assertTrue(payload.exists())

    def test_tampered_manifest_cannot_restore_generated_bytes_over_an_upload(self):
        now = datetime.now(timezone.utc)
        orphan = self._old_orphan("manifest", now=now)
        candidate = next(item for item in scan_artifacts(self.store, self.root, now=now)
                         if item.get("path") == orphan.relative_to(self.root).as_posix())
        record = quarantine_artifact(self.store, self.root, candidate["artifact_id"], now=now)
        manifest_path = self.root / ".quarantine" / f"{record['quarantine_id']}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["original_path"] = "uploads/original.txt"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "managed generated artifact"):
            restore_quarantined(self.store, self.root, record["quarantine_id"], now=now)
        self.assertTrue((self.root / ".quarantine" / record["quarantine_id"] /
                         "artifact" / "resume.txt").exists())


if __name__ == "__main__":
    unittest.main()
