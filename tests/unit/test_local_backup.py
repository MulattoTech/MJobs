"""Offline backup validation protects a fresh-project restore."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from cli import local_backup


ROOT = Path(__file__).resolve().parents[2]


class LocalBackupTests(unittest.TestCase):
    @staticmethod
    def archive_fixture(archive):
        (archive / "documents").mkdir()
        (archive / "database.dump").write_bytes(b"synthetic-postgres-dump")
        (archive / "documents" / "resume.pdf").write_bytes(b"synthetic-document")
        files = {
            name: hashlib.sha256((archive / name).read_bytes()).hexdigest()
            for name in ("database.dump", "documents/resume.pdf")
        }
        manifest = {
            "schema_version": 1,
            "source_project": "synthetic-source",
            "operation_mode": "PERSONAL",
            "alembic_revision": "synthetic-revision",
            "compose_sha256": hashlib.sha256((ROOT / "compose.yml").read_bytes()).hexdigest(),
            "files": files,
        }
        (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def test_verify_accepts_complete_untampered_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary)
            self.archive_fixture(archive)
            result = subprocess.run(
                [sys.executable, "-m", "cli.local_backup", "verify", "--archive", str(archive)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Backup verified", result.stdout)

    def test_verify_rejects_tampered_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary)
            self.archive_fixture(archive)
            (archive / "documents" / "resume.pdf").write_bytes(b"changed-document")
            with self.assertRaisesRegex(local_backup.BackupError, "checksum mismatch"):
                local_backup.verify_backup(archive)

    def test_verify_rejects_unlisted_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary)
            self.archive_fixture(archive)
            (archive / "documents" / "unlisted.txt").write_bytes(b"extra")
            with self.assertRaisesRegex(local_backup.BackupError, "unlisted"):
                local_backup.verify_backup(archive)

    def test_restored_document_tree_refuses_extra_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = root / "documents"
            documents.mkdir()
            (documents / "resume.pdf").write_bytes(b"synthetic-document")
            (documents / "unexpected-session.txt").write_bytes(b"extra")
            manifest = {"files": {"documents/resume.pdf": hashlib.sha256(b"synthetic-document").hexdigest()}}
            self.assertTrue(hasattr(local_backup, "verify_document_tree"), "Restored-volume inventory check is missing")
            with self.assertRaisesRegex(local_backup.BackupError, "unlisted"):
                local_backup.verify_document_tree(manifest, root)

    def test_backup_refuses_any_running_writer(self):
        states = {"api": "exited", "worker": "exited", "source-worker": "running",
                  "document-worker": "exited", "postgres": "running"}
        self.assertTrue(hasattr(local_backup, "require_stopped_writers"), "Offline backup preflight is missing")
        with self.assertRaisesRegex(local_backup.BackupError, "source-worker"):
            local_backup.require_stopped_writers(states)

    def test_backup_requires_scheduler_and_company_discovery_to_be_quiescent(self):
        states = {service: "exited" for service in (
            "api", "worker", "source-worker", "document-worker", "ai-worker", "migrate",
            "scheduler", "company-discovery-worker")}
        states["postgres"] = "running"
        for service in ("scheduler", "company-discovery-worker", "ai-worker"):
            for state in ("running", "restarting", None):
                with self.subTest(service=service, state=state):
                    unsafe = dict(states)
                    unsafe[service] = state
                    with self.assertRaisesRegex(local_backup.BackupError, service):
                        local_backup.require_stopped_writers(unsafe)
        local_backup.require_stopped_writers(states)

    def test_restore_refuses_any_existing_target_volume(self):
        self.assertTrue(hasattr(local_backup, "require_empty_target"), "Fresh-target guard is missing")
        with self.assertRaisesRegex(local_backup.BackupError, "already exists"):
            local_backup.require_empty_target("mjobs-recovered", "mjobs-source", [],
                                              ["mjobs-recovered_database"])

    def test_backup_refuses_existing_destination_before_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "existing-backup"
            destination.mkdir()
            result = subprocess.run(
                [sys.executable, "-m", "cli.local_backup", "backup", "--project", "synthetic-source",
                 "--archive", str(destination)], cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("already exists", result.stderr)

    def test_captured_compose_output_is_bounded_and_sanitized(self):
        marker = "PRIVATE_DIAGNOSTIC_SENTINEL"
        with tempfile.TemporaryDirectory() as temporary:
            completion = Path(temporary) / "child-finished"
            script = ("import pathlib,sys,time; sys.stdout.write(" + repr(marker) +
                      " * 100000); sys.stdout.flush(); time.sleep(1); pathlib.Path(" +
                      repr(str(completion)) + ").write_text('finished')")
            command = [sys.executable, "-c", script]
            with self.assertRaisesRegex(local_backup.BackupError, "output limit") as error:
                local_backup._run(command, timeout=5, action="Inspect project")
            self.assertNotIn(marker, str(error.exception))
            time.sleep(0.05)
            self.assertFalse(completion.exists(), "oversized child continued after the output limit")

    def test_large_database_dump_stream_is_not_counted_as_captured_metadata(self):
        payload = b"D" * (local_backup.MAX_CAPTURED_STDOUT_BYTES + 1)
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'D' * " +
                   str(len(payload)) + ")"]
        with tempfile.TemporaryFile(mode="w+b") as dump:
            result = local_backup._run(command, stdout=dump, timeout=5, action="Write database dump")
            dump.seek(0, 2)
            self.assertEqual(dump.tell(), len(payload))
        self.assertEqual(result, b"")

    def test_process_timeout_is_classified_without_echoing_child_output(self):
        marker = "PRIVATE_TIMEOUT_SENTINEL"
        command = [sys.executable, "-c",
                   "import sys,time; print(" + repr(marker) + ",flush=True); time.sleep(5)"]
        with self.assertRaisesRegex(local_backup.BackupError, "could not complete") as error:
            local_backup._run(command, timeout=0.1, action="Inspect project")
        self.assertNotIn(marker, str(error.exception))

    def test_large_stderr_is_drained_without_becoming_returned_output(self):
        command = [sys.executable, "-c", "import os; os.write(2,b'X'*8388608); os.write(1,b'ok')"]
        self.assertEqual(local_backup._run(command, timeout=5, action="Inspect project"), b"ok")


if __name__ == "__main__":
    unittest.main()
