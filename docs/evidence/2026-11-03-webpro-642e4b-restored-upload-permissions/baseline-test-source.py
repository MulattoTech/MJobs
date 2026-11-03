"""Fresh restore targets apply the private uploaded-original policy before startup."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from cli import local_backup
from tests.unit.test_local_backup import LocalBackupTests


class RestoredUploadPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-restore-privacy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def tree_state(root):
        return {
            path.relative_to(root).as_posix(): (
                stat.S_IMODE(path.stat().st_mode),
                path.read_bytes() if path.is_file() else None,
            )
            for path in (root, *sorted(root.rglob("*")))
        }

    def archive_fixture(self):
        archive = self.root / "archive"
        archive.mkdir()
        manifest = LocalBackupTests.archive_fixture(archive)
        uploads = archive / "documents" / "uploads"
        (uploads / "nested").mkdir(parents=True)
        for directory in (uploads, uploads / "nested"):
            directory.chmod(0o700)
        for relative, contents, mode in (
            ("original.txt", b"synthetic original for restore", 0o400),
            ("nested/older.txt", b"synthetic prior original", 0o600),
        ):
            path = uploads / relative
            path.write_bytes(contents)
            path.chmod(mode)
            manifest["files"][path.relative_to(archive).as_posix()] = hashlib.sha256(contents).hexdigest()
        (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return archive, manifest

    def restore_fixture(self, archive, manifest, target, *, fail_preparation=False):
        events = []
        observations = {}
        def compose(_project, *arguments, **kwargs):
            if "prepare-upload-permissions" in arguments:
                events.append("prepare")
                self.assertIn("--user", arguments)
                self.assertEqual(arguments[arguments.index("--user") + 1], "10001:10001")
                self.assertEqual(json.load(kwargs["stdin"]), manifest)
                if fail_preparation:
                    raise local_backup.BackupError("Synthetic preparation failure")
                local_backup.prepare_restored_upload_permissions(manifest, target)
            if "verify-documents" in arguments:
                events.append("verify")
                self.assertEqual(json.load(kwargs["stdin"]), manifest)
                self.assertEqual(stat.S_IMODE((target / "documents/uploads").stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((target / "documents/uploads/original.txt").stat().st_mode), 0o600)
                local_backup.verify_document_tree(manifest, target)
            if "up" in arguments and "--no-build" in arguments:
                events.append("start")
            if "chown" in arguments:
                events.append("chown")
            return b""
        def run(arguments, **_kwargs):
            if arguments[:2] == ["docker", "cp"]:
                source = Path(arguments[2])
                observations["snapshot_upload_mode"] = stat.S_IMODE((source / "uploads").stat().st_mode)
                observations["snapshot_original_mode"] = stat.S_IMODE((source / "uploads/original.txt").stat().st_mode)
                observations["snapshot"] = source.parent
                shutil.copytree(source, target / "documents", copy_function=shutil.copyfile)
                events.append("copy")
            return b""
        with patch.object(local_backup, "_config", return_value={}), \
             patch.object(local_backup, "_compose", side_effect=compose), \
             patch.object(local_backup, "_run", side_effect=run), \
             patch.object(local_backup, "_sql", side_effect=("0", "synthetic-revision", "PERSONAL")), \
             patch.object(local_backup, "_volumes", return_value=[]), \
             patch.object(local_backup, "_container", return_value="synthetic-api"), \
             patch.object(local_backup, "_wait_ready", side_effect=lambda _port: events.append("ready")):
            local_backup.restore_backup(archive, "synthetic-restored", 18712)
        return events, observations

    @unittest.skipUnless(os.name == "posix", "POSIX permission behavior requires Linux execution")
    def test_real_snapshot_copy_is_private_before_readonly_verification_and_startup(self):
        archive, manifest = self.archive_fixture()
        before = self.tree_state(archive)
        target = self.root / "target"
        target.mkdir()
        previous_umask = os.umask(0o022)
        try:
            events, observed = self.restore_fixture(archive, manifest, target)
        finally:
            os.umask(previous_umask)
            self.assertEqual(self.tree_state(archive), before)
        # The host snapshot is byte-only and can broaden modes. The new target
        # must explicitly normalize policy after copy/chown, including Windows copies.
        self.assertEqual(observed["snapshot_upload_mode"], 0o755)
        self.assertEqual(observed["snapshot_original_mode"], 0o644)
        self.assertEqual(events, ["copy", "chown", "prepare", "verify", "start", "ready"])
        self.assertFalse(observed["snapshot"].exists())
        self.assertEqual(stat.S_IMODE((target / "documents").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((target / "documents/resume.pdf").stat().st_mode), 0o644)
        self.assertEqual((target / "documents/uploads/original.txt").read_bytes(),
                         b"synthetic original for restore")
        self.assertEqual((target / "documents/uploads/nested/older.txt").read_bytes(),
                         b"synthetic prior original")
        self.assertEqual(stat.S_IMODE((target / "documents/uploads/nested").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((target / "documents/uploads/nested/older.txt").stat().st_mode), 0o600)
