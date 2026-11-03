"""Fresh restore targets apply the private uploaded-original policy before startup."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from cli import local_backup
from tests.unit import test_local_backup as fixtures


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
        manifest = fixtures.LocalBackupTests.archive_fixture(archive)
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
            if os.name == "posix":
                path.chmod(mode)
            manifest["files"][path.relative_to(archive).as_posix()] = hashlib.sha256(contents).hexdigest()
        (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return archive, manifest

    def restore_fixture(self, archive, manifest, target, *, fail_preparation=False, events=None):
        if events is None:
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

    def target_fixture(self, label="target"):
        target = self.root / label
        uploads = target / "documents" / "uploads"
        uploads.mkdir(parents=True)
        (uploads / "original.txt").write_bytes(b"synthetic original")
        (target / "documents/generated.txt").write_bytes(b"synthetic generated sibling")
        if os.name == "posix":
            for path in (target / "documents", uploads):
                path.chmod(0o755)
            for path in (uploads / "original.txt", target / "documents/generated.txt"):
                path.chmod(0o644)
        manifest = {"files": {
            path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in target.rglob("*") if path.is_file()
        }}
        return target, manifest

    def test_preparation_failure_prevents_verification_startup_and_readiness(self):
        archive, manifest = self.archive_fixture()
        before = self.tree_state(archive)
        target = self.root / "failed-target"
        target.mkdir()
        events = []
        with self.assertRaisesRegex(local_backup.BackupError, "^Synthetic preparation failure$"):
            self.restore_fixture(archive, manifest, target, fail_preparation=True, events=events)
        self.assertEqual(events, ["copy", "chown", "prepare"])
        self.assertEqual(self.tree_state(archive), before)

    @unittest.skipUnless(os.name == "posix", "POSIX permission behavior requires Linux execution")
    def test_normalization_keeps_bytes_parent_generated_and_archive_unchanged(self):
        archive, archive_manifest = self.archive_fixture()
        archive_before = self.tree_state(archive)
        target, manifest = self.target_fixture()
        before = self.tree_state(target)
        uploads = target / "documents/uploads"
        (uploads / "empty").mkdir(mode=0o755)
        local_backup.prepare_restored_upload_permissions(manifest, target)
        after = self.tree_state(target)
        for name in ("documents", "documents/generated.txt", "."):
            self.assertEqual(after[name], before[name])
        self.assertEqual(after["documents/uploads/original.txt"], (0o600, b"synthetic original"))
        self.assertEqual(after["documents/uploads"][0], 0o700)
        self.assertEqual(after["documents/uploads/empty"][0], 0o700)
        self.assertEqual(self.tree_state(archive), archive_before)
        self.assertEqual(local_backup.verify_backup(archive), archive_manifest)
        local_backup.verify_document_tree(manifest, target)

    @unittest.skipUnless(os.name == "posix", "POSIX permission behavior requires Linux execution")
    def test_missing_uploads_is_a_verified_noop(self):
        target, manifest = self.target_fixture()
        (target / "documents/uploads/original.txt").unlink()
        (target / "documents/uploads").rmdir()
        del manifest["files"]["documents/uploads/original.txt"]
        before = self.tree_state(target)
        with patch.object(local_backup.os, "fchmod") as chmod:
            local_backup.prepare_restored_upload_permissions(manifest, target)
        chmod.assert_not_called()
        self.assertEqual(self.tree_state(target), before)

    @unittest.skipUnless(os.name == "posix", "POSIX permission behavior requires Linux execution")
    def test_existing_document_verification_stays_readonly(self):
        target, manifest = self.target_fixture()
        before = self.tree_state(target)
        with patch.object(local_backup.os, "fchmod") as chmod:
            local_backup.verify_document_tree(manifest, target)
        chmod.assert_not_called()
        self.assertEqual(self.tree_state(target), before)

    @unittest.skipUnless(os.name == "posix", "POSIX permission behavior requires Linux execution")
    def test_invalid_inventory_is_rejected_before_any_mode_change(self):
        for invalid in ("extra", "missing", "checksum"):
            with self.subTest(invalid=invalid):
                target, manifest = self.target_fixture(invalid)
                original = target / "documents/uploads/original.txt"
                if invalid == "extra":
                    (target / "documents/extra.txt").write_bytes(b"synthetic extra")
                elif invalid == "missing":
                    original.unlink()
                else:
                    original.write_bytes(b"synthetic changed bytes")
                before = self.tree_state(target)
                with patch.object(local_backup.os, "fchmod") as chmod:
                    with self.assertRaisesRegex(local_backup.BackupError,
                                                "^Restored upload permissions could not be prepared$"):
                        local_backup.prepare_restored_upload_permissions(manifest, target)
                chmod.assert_not_called()
                self.assertEqual(self.tree_state(target), before)

    @unittest.skipUnless(os.name == "posix", "POSIX filesystem nodes require Linux execution")
    def test_links_and_fifo_are_rejected_before_any_mode_change(self):
        for kind in ("symlink", "directory-symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                target, manifest = self.target_fixture(kind)
                original = target / "documents/uploads/original.txt"
                outside = self.root / ("outside-" + kind)
                outside.mkdir()
                sentinel = outside / "sentinel.txt"
                sentinel.write_bytes(b"synthetic original")
                sentinel.chmod(0o400)
                if kind == "directory-symlink":
                    (target / "documents/uploads/linked").symlink_to(outside, target_is_directory=True)
                else:
                    original.unlink()
                    if kind == "symlink":
                        original.symlink_to(sentinel)
                    elif kind == "hardlink":
                        os.link(sentinel, original)
                    else:
                        os.mkfifo(original)
                outside_before = self.tree_state(outside)
                with patch.object(local_backup.os, "fchmod") as chmod:
                    with self.assertRaisesRegex(local_backup.BackupError,
                                                "^Restored upload permissions could not be prepared$"):
                        local_backup.prepare_restored_upload_permissions(manifest, target)
                chmod.assert_not_called()
                self.assertEqual(stat.S_IMODE((target / "documents/uploads").stat().st_mode), 0o755)
                self.assertEqual(self.tree_state(outside), outside_before)
                if kind == "fifo":
                    self.assertTrue(stat.S_ISFIFO(original.stat().st_mode))

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor behavior requires Linux execution")
    def test_fifo_substituted_after_inventory_is_nonblocking_and_never_chmodded(self):
        target, manifest = self.target_fixture()
        original = target / "documents/uploads/original.txt"
        verify = local_backup.verify_document_tree
        real_open = os.open
        opened = []
        calls = []
        def inventory_then_fifo(*arguments):
            verify(*arguments)
            original.unlink()
            os.mkfifo(original)
        def opened_node(path, flags, *arguments, **kwargs):
            # Fail before opening the FIFO if a future change drops either flag.
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_NONBLOCK)
            descriptor = real_open(path, flags, *arguments, **kwargs)
            opened.append(descriptor)
            calls.append((path, flags, kwargs.get("dir_fd")))
            return descriptor
        with patch.object(local_backup, "verify_document_tree", side_effect=inventory_then_fifo), \
             patch.object(local_backup.os, "open", side_effect=opened_node) as descriptor_open, \
             patch.object(local_backup.os, "fchmod") as chmod:
            with patch.object(local_backup.os, "supports_dir_fd", os.supports_dir_fd | {descriptor_open}):
                with self.assertRaisesRegex(local_backup.BackupError,
                                            "^Restored upload permissions could not be prepared$"):
                    local_backup.prepare_restored_upload_permissions(manifest, target)
        chmod.assert_not_called()
        self.assertTrue(calls)
        self.assertTrue(all(flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK for _, flags, _ in calls))
        leaf_calls = [call for call in calls if call[0] == "original.txt"]
        self.assertEqual(len(leaf_calls), 1)
        self.assertIsInstance(leaf_calls[0][2], int)
        for descriptor in set(opened):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor behavior requires Linux execution")
    def test_permission_failure_and_failed_mode_readback_close_descriptors(self):
        for outcome in ("permission-error", "unchanged-mode"):
            with self.subTest(outcome=outcome):
                target, manifest = self.target_fixture(outcome)
                before = self.tree_state(target)
                real_open = os.open
                opened = []
                def opened_node(*arguments, **kwargs):
                    descriptor = real_open(*arguments, **kwargs)
                    opened.append(descriptor)
                    return descriptor
                failure = PermissionError("synthetic-private-machine-path") if outcome == "permission-error" else None
                with patch.object(local_backup.os, "open", side_effect=opened_node) as descriptor_open, \
                     patch.object(local_backup.os, "fchmod", side_effect=failure):
                    with patch.object(local_backup.os, "supports_dir_fd", os.supports_dir_fd | {descriptor_open}):
                        with self.assertRaisesRegex(local_backup.BackupError,
                                                    "^Restored upload permissions could not be prepared$"):
                            local_backup.prepare_restored_upload_permissions(manifest, target)
                self.assertEqual(self.tree_state(target), before)
                for descriptor in set(opened):
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

    def test_missing_platform_facilities_fail_closed_before_inventory(self):
        cases = (
            ("name", "not-posix"),
            ("supports_dir_fd", set()),
            ("supports_fd", set()),
            ("fchmod", None),
            ("O_NOFOLLOW", 0),
            ("O_NONBLOCK", 0),
            ("O_DIRECTORY", 0),
        )
        for name, value in cases:
            with self.subTest(facility=name):
                with patch.object(local_backup.os, name, value, create=True), \
                     patch.object(local_backup, "verify_document_tree") as inventory:
                    with self.assertRaisesRegex(local_backup.BackupError,
                                                "^Restored upload permissions could not be prepared$"):
                        local_backup.prepare_restored_upload_permissions({"files": {}}, self.root)
                inventory.assert_not_called()

    def test_runtime_cli_dispatches_explicit_mutation_with_manifest_and_volume_root(self):
        manifest = {"files": {}}
        output = io.StringIO()
        with patch.object(local_backup.sys, "argv", ["local_backup", "prepare-upload-permissions"]), \
             patch.object(local_backup.sys, "stdin", io.StringIO(json.dumps(manifest))), \
             patch.object(local_backup, "prepare_restored_upload_permissions") as prepare, \
             redirect_stdout(output):
            self.assertEqual(local_backup.main(), 0)
        prepare.assert_called_once_with(manifest, Path("/data"))
        self.assertEqual(output.getvalue(), "Restored upload permissions prepared\n")

    def test_runtime_cli_rejects_invalid_json_without_echoing_input(self):
        error = io.StringIO()
        with patch.object(local_backup.sys, "argv", ["local_backup", "prepare-upload-permissions"]), \
             patch.object(local_backup.sys, "stdin", io.StringIO("synthetic-private-invalid-json")), \
             patch.object(local_backup, "prepare_restored_upload_permissions") as prepare, \
             redirect_stderr(error):
            with self.assertRaises(SystemExit) as failure:
                local_backup.main()
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("Restored upload permissions could not be prepared", error.getvalue())
        self.assertNotIn("synthetic-private", error.getvalue())
        prepare.assert_not_called()
