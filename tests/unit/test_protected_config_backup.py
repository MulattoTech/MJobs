"""Authenticated configuration recovery, privacy, bounds, and no-clobber writes."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cli import protected_config_backup as backup


DEPENDENCIES = all(importlib.util.find_spec(name) is not None for name in ("sqlalchemy", "alembic"))
KEY = b"0123456789abcdef0123456789abcdef"
CANARY = "synthetic-protected-configuration-canary"
FILES = {".env": ("SECRET=" + CANARY + "\r\n").encode(),
         "compose.private.yml": b"services:\n  api:\n    environment:\n      PRIVATE: synthetic\n\x00\xff"}


def raw_envelope(payload, key=KEY):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    nonce = b"\x17" * backup.NONCE_BYTES
    return backup.MAGIC + nonce + AESGCM(key).encrypt(nonce, data, backup.MAGIC)


class ProtectedConfigFormatTests(unittest.TestCase):
    def test_exact_arbitrary_bytes_roundtrip_with_real_aes256_gcm(self):
        envelope = backup._encrypt(FILES, KEY)
        self.assertEqual(backup._decrypt(envelope, KEY), FILES)
        self.assertNotIn(CANARY.encode(), envelope)
        self.assertNotIn(b"compose.private.yml", envelope)
        self.assertNotIn(KEY, envelope)

    def test_each_backup_uses_a_new_nonce(self):
        self.assertNotEqual(backup._encrypt(FILES, KEY), backup._encrypt(FILES, KEY))

    def test_wrong_key_and_changed_header_nonce_ciphertext_tag_or_length_refuse(self):
        envelope = backup._encrypt(FILES, KEY)
        for offset in (0, len(backup.MAGIC), len(backup.MAGIC) + backup.NONCE_BYTES, len(envelope) - 1):
            changed = bytearray(envelope)
            changed[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(backup.ProtectedConfigError):
                backup._decrypt(bytes(changed), KEY)
        for changed in (b"", envelope[:10], envelope[:-1], envelope + b"x"):
            with self.subTest(length=len(changed)), self.assertRaises(backup.ProtectedConfigError):
                backup._decrypt(changed, KEY)
        with self.assertRaises(backup.ProtectedConfigError):
            backup._decrypt(envelope, b"w" * 32)

    def test_non_256_bit_recovery_keys_are_rejected(self):
        for key in (b"k" * 16, b"k" * 24, b"k" * 31, b"k" * 33, "k" * 32):
            with self.subTest(length=len(key)), self.assertRaises(backup.ProtectedConfigError):
                backup._encrypt(FILES, key)
            with self.subTest(decrypt_length=len(key)), self.assertRaises(backup.ProtectedConfigError):
                backup._decrypt(b"untrusted", key)

    def test_strict_flat_names_and_file_count(self):
        unsafe = ("../.env", "/.env", "C:\\.env", ".env:stream", "compose.private.yml:stream",
                  "compose.private.yml.", "compose.private.yml ", "compose..yml", "CON", "NUL",
                  "sub/compose.private.yml", "sub\\compose.private.yml", "compose.yml", "*.env",
                  "COMPOSE.private.yml", "private.json", "", 1)
        for name in unsafe:
            with self.subTest(name=name), self.assertRaises(backup.ProtectedConfigError):
                backup._names([name])
        for names in ([], [".env", ".env"], [f"compose.file{number}.yml" for number in range(9)]):
            with self.subTest(names=names), self.assertRaises(backup.ProtectedConfigError):
                backup._names(names)
        self.assertEqual(backup._names([".env", "compose.personal.yaml"]), (".env", "compose.personal.yaml"))

    def test_authenticated_malformed_payloads_still_fail_validation(self):
        valid = {"schema_version": 1, "files": [{"name": ".env", "data": ""}]}
        malformed = [None, [], {}, {**valid, "unknown": 1}, {**valid, "schema_version": True},
                     {**valid, "schema_version": 2}, {**valid, "files": []},
                     {**valid, "files": [{"name": "../.env", "data": ""}]},
                     {**valid, "files": [{"name": ".env", "data": "@@@"}]},
                     {**valid, "files": [{"name": ".env", "data": 7}]},
                     {**valid, "files": [{"name": ".env", "data": "", "extra": 1}]},
                     {**valid, "files": [{"name": ".env"}]},
                     {**valid, "files": [False]},
                     {**valid, "files": valid["files"] * 2},
                     b'{"schema_version":1,"schema_version":1,"files":[{"name":".env","data":""}]}',
                     b'{"schema_version":1,"files":[{"name":".env","name":".env","data":""}]}']
        for number, payload in enumerate(malformed):
            with self.subTest(case=number), self.assertRaises(backup.ProtectedConfigError):
                backup._decrypt(raw_envelope(payload), KEY)

    def test_file_total_and_envelope_bounds_apply_before_acceptance(self):
        with self.assertRaises(backup.ProtectedConfigError):
            backup._encrypt({".env": b"x" * (backup.MAX_FILE_BYTES + 1)}, KEY)
        oversized = {f"compose.private{i}.yml": b"x" * backup.MAX_FILE_BYTES for i in range(5)}
        with self.assertRaises(backup.ProtectedConfigError):
            backup._encrypt(oversized, KEY)
        with self.assertRaises(backup.ProtectedConfigError):
            backup._decrypt(b"x" * (backup.MAX_ARCHIVE_BYTES + 1), KEY)
        payload = {"schema_version": 1, "files": [{"name": ".env", "data":
                   base64.b64encode(b"x" * (backup.MAX_FILE_BYTES + 1)).decode()}]}
        with self.assertRaises(backup.ProtectedConfigError):
            backup._decrypt(raw_envelope(payload), KEY)
        oversized_payload = {"schema_version": 1, "files": [
            {"name": name, "data": base64.b64encode(value).decode()} for name, value in oversized.items()]}
        with self.assertRaises(backup.ProtectedConfigError):
            backup._decrypt(raw_envelope(oversized_payload), KEY)

    def test_no_filesystem_parent_traversal_or_colocated_recovery_key(self):
        with self.assertRaises(backup.ProtectedConfigError):
            backup._path(Path.cwd() / "a" / ".." / "secret")
        root = Path.cwd()
        for key, archive, source in ((root / "key", root / "archive", None),
                                     (root / "keys/key", root / "keys/nested/archive", None),
                                     (root / "source/key", root / "archives/archive", root / "source")):
            with self.subTest(case=str(key)), self.assertRaises(backup.ProtectedConfigError):
                backup._separate(key, archive, source)

    def test_cli_error_boundary_never_prints_secret_bearing_exception(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(backup, "backup_config", side_effect=OSError(CANARY)), \
                redirect_stdout(output), redirect_stderr(error):
            result = backup.main(["backup", "--source-root", "synthetic", "--file", ".env",
                                  "--key-file", "synthetic-key", "--archive", "synthetic-archive"])
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(CANARY, error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())


class ProtectedConfigSnapshotTests(unittest.TestCase):
    """Synthetic metadata reproduces Windows path/descriptor ctime differences."""

    def read_with_metadata(self, platform, *, change_path=False, change_descriptor=False):
        path_ctime = 1793632679792349500
        descriptor_ctime = 1793632679792888700

        def metadata(ctime):
            return SimpleNamespace(st_dev=12832129363191362353, st_ino=562949960180268,
                                   st_mode=33206, st_nlink=1, st_size=9,
                                   st_mtime_ns=1793632679792888700, st_ctime_ns=ctime)

        before_path = metadata(path_ctime)
        after_path = metadata(path_ctime + int(change_path))
        opened = metadata(descriptor_ctime)
        after_descriptor = metadata(descriptor_ctime + int(change_descriptor))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-file"
            path.write_bytes(b"synthetic")
            with patch.object(backup.os, "name", platform), patch.object(backup, "_private_file"), \
                    patch.object(Path, "lstat", side_effect=(before_path, after_path)), \
                    patch.object(backup.os, "fstat", side_effect=(opened, after_descriptor)):
                data, snapshot = backup._read_snapshot(path, 32)
        return data, snapshot, after_path

    def test_stable_windows_file_accepts_cross_api_ctime_difference_and_returns_path_snapshot(self):
        data, snapshot, final_path = self.read_with_metadata("nt")
        self.assertEqual(data, b"synthetic")
        self.assertIs(snapshot, final_path)

    def test_windows_ctime_changes_within_each_api_are_still_rejected(self):
        for changed in ("change_path", "change_descriptor"):
            with self.subTest(changed=changed), self.assertRaises(backup.ProtectedConfigError):
                self.read_with_metadata("nt", **{changed: True})

    def test_posix_keeps_ctime_in_cross_api_comparison(self):
        with self.assertRaises(backup.ProtectedConfigError):
            self.read_with_metadata("posix")


class ProtectedConfigPublicationTests(unittest.TestCase):
    """Real filesystem races with only the unrelated ACL policy boundary stubbed.

    Native private-file/NTFS acceptance belongs to the filesystem tests below.
    These lifecycle regressions also run when database imports are unavailable.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("source", "keys", "archives", "recovery"):
            (self.root / name).mkdir(mode=0o700)
        self.enterContext(patch.object(backup, "_private_parent"))
        self.enterContext(patch.object(backup, "_private_file"))

    def test_staging_unlink_failure_cannot_report_success_with_an_unreadable_output(self):
        destination = self.root / "keys/recovery.key"
        unlink = Path.unlink

        def refuse_staging(path, *args, **kwargs):
            if path.name.startswith(".mjobs-config-"):
                raise PermissionError(CANARY)
            return unlink(path, *args, **kwargs)

        output, error = io.StringIO(), io.StringIO()
        with patch.object(Path, "unlink", refuse_staging), redirect_stdout(output), redirect_stderr(error):
            result = backup.main(["keygen", "--key-file", str(destination)])
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(CANARY, error.getvalue())
        self.assertFalse(destination.exists())
        remaining = list(destination.parent.glob(".mjobs-config-*.tmp"))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].stat().st_nlink, 1)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(remaining[0].stat().st_mode), 0o600)

    def test_restore_never_adopts_a_post_publication_replacement_as_its_own_file(self):
        source = self.root / "source"
        for name, value in FILES.items():
            (source / name).write_bytes(value)
        key = self.root / "keys/recovery.key"
        archive = self.root / "archives/config.mjpc"
        backup.generate_key(key)
        backup.backup_config(source, list(FILES), key, archive)
        publish = backup._publish_new
        for fail_later in (False, True):
            with self.subTest(later_failure=fail_later):
                destination = self.root / "recovery" / str(fail_later)

                def replace_published(path, data):
                    if fail_later and path.name == "compose.private.yml":
                        raise OSError(CANARY)
                    owned_identity = publish(path, data)
                    if path.name == ".env":
                        # Allocate the winning inode before replacing the old
                        # file so the ownership race is deterministic.
                        competitor = destination / "competing-file"
                        competitor.write_bytes(b"foreign-winning-configuration")
                        os.replace(competitor, path)
                    return owned_identity

                with patch.object(backup, "_publish_new", side_effect=replace_published), \
                        self.assertRaises(backup.ProtectedConfigError):
                    backup.restore_config(archive, key, destination)
                self.assertEqual((destination / ".env").read_bytes(), b"foreign-winning-configuration")


@unittest.skipUnless(DEPENDENCIES, "Private filesystem guards require installed SQLAlchemy and Alembic")
class ProtectedConfigFilesystemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.keys, self.archives, self.recovery = [self.root / name for name in
                                                            ("source", "keys", "archives", "recovery")]
        for path in (self.source, self.keys, self.archives, self.recovery):
            path.mkdir(mode=0o700)
        self.key = self.keys / "recovery.key"
        self.archive = self.archives / "config.mjpc"
        self.destination = self.recovery / "restored"
        for name, value in FILES.items():
            self.write_private(self.source / name, value)
        backup.generate_key(self.key)

    @staticmethod
    def write_private(path, data):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)

    def create_archive(self):
        return backup.backup_config(self.source, list(FILES), self.key, self.archive)

    def test_roundtrip_real_files_are_exact_private_and_separate(self):
        self.assertEqual(self.create_archive(), 2)
        self.assertEqual(backup.verify_archive(self.archive, self.key), 2)
        self.assertFalse(self.destination.exists())
        self.assertEqual(backup.restore_config(self.archive, self.key, self.destination), 2)
        self.assertEqual({path.name: path.read_bytes() for path in self.destination.iterdir()}, FILES)
        self.assertNotIn(CANARY.encode(), self.archive.read_bytes())
        self.assertEqual(len(self.key.read_bytes()), 32)
        backup._private_parent(self.destination)
        for path in (self.key, self.archive, *self.destination.iterdir()):
            backup._private_file(path)
            self.assertEqual(path.stat().st_nlink, 1)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)

    def test_cli_success_reports_counts_without_configuration_or_keys(self):
        key = self.keys / "cli.key"
        arguments = [
            ["keygen", "--key-file", str(key)],
            ["backup", "--source-root", str(self.source), "--file", ".env", "--file", "compose.private.yml",
             "--key-file", str(key), "--archive", str(self.archive)],
            ["verify", "--key-file", str(key), "--archive", str(self.archive)],
            ["restore", "--key-file", str(key), "--archive", str(self.archive),
             "--destination", str(self.destination)],
        ]
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            for argv in arguments:
                self.assertEqual(backup.main(argv), 0)
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(len(output.getvalue().splitlines()), 4)
        self.assertNotIn(CANARY, output.getvalue())
        self.assertNotIn(base64.b64encode(key.read_bytes()).decode(), output.getvalue())
        self.assertEqual((self.destination / ".env").read_bytes(), FILES[".env"])

    def test_existing_key_archive_and_restore_target_are_never_overwritten(self):
        original_key = self.key.read_bytes()
        with self.assertRaises(backup.ProtectedConfigError):
            backup.generate_key(self.key)
        self.assertEqual(self.key.read_bytes(), original_key)
        self.create_archive()
        original_archive = self.archive.read_bytes()
        with self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()
        self.assertEqual(self.archive.read_bytes(), original_archive)
        self.destination.mkdir(mode=0o700)
        sentinel = self.destination / "sentinel"
        sentinel.write_bytes(b"existing")
        with self.assertRaises(backup.ProtectedConfigError):
            backup.restore_config(self.archive, self.key, self.destination)
        self.assertEqual(sentinel.read_bytes(), b"existing")

    def test_concurrent_output_publication_preserves_winner_and_removes_only_own_temp(self):
        destination = self.keys / "competing.key"
        link = os.link

        def competitor(source, target, **kwargs):
            self.write_private(Path(target), b"winning-existing-key")
            link(source, target, **kwargs)

        with patch.object(backup.os, "link", side_effect=competitor), self.assertRaises(backup.ProtectedConfigError):
            backup.generate_key(destination)
        self.assertEqual(destination.read_bytes(), b"winning-existing-key")
        self.assertEqual(list(self.keys.glob(".mjobs-config-*.tmp")), [])

    def test_wrong_key_corruption_and_invalid_authenticated_paths_create_no_restore_directory(self):
        self.create_archive()
        original = self.archive.read_bytes()
        wrong = self.keys / "wrong.key"
        backup.generate_key(wrong)
        with self.assertRaises(backup.ProtectedConfigError):
            backup.restore_config(self.archive, wrong, self.destination)
        self.assertFalse(self.destination.exists())
        invalid = raw_envelope({"schema_version": 1, "files": [{"name": "../escape", "data": ""}]},
                               self.key.read_bytes())
        for value in (original[:-1], original[:-1] + bytes([original[-1] ^ 1]), invalid):
            self.archive.write_bytes(value)
            with self.subTest(length=len(value)), self.assertRaises(backup.ProtectedConfigError):
                backup.restore_config(self.archive, self.key, self.destination)
            self.assertFalse(self.destination.exists())
        with self.archive.open("wb") as stream:
            stream.truncate(backup.MAX_ARCHIVE_BYTES + 1)
        with self.assertRaises(backup.ProtectedConfigError):
            backup.restore_config(self.archive, self.key, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.recovery / "escape").exists())

    def test_restore_write_failure_cleans_only_owned_files_and_directory(self):
        self.create_archive()
        publish = backup._publish_new

        def fail_second(path, data):
            if path.name == "compose.private.yml":
                raise OSError(CANARY)
            return publish(path, data)

        with patch.object(backup, "_publish_new", side_effect=fail_second), self.assertRaises(backup.ProtectedConfigError) as error:
            backup.restore_config(self.archive, self.key, self.destination)
        self.assertNotIn(CANARY, str(error.exception))
        self.assertFalse(self.destination.exists())

    def test_restore_file_race_preserves_competing_file(self):
        self.create_archive()
        publish = backup._publish_new

        def compete(path, data):
            self.write_private(path, b"foreign-winning-file")
            return publish(path, data)

        with patch.object(backup, "_publish_new", side_effect=compete), self.assertRaises(backup.ProtectedConfigError):
            backup.restore_config(self.archive, self.key, self.destination)
        self.assertEqual((self.destination / ".env").read_bytes(), b"foreign-winning-file")

    def test_private_check_happens_before_secret_write(self):
        destination = self.keys / "unavailable.key"
        inspected = []

        def refuse(path):
            inspected.append(path.read_bytes())
            raise OSError(CANARY)

        with patch.object(backup, "_private_file", side_effect=refuse), self.assertRaises(backup.ProtectedConfigError):
            backup.generate_key(destination)
        self.assertEqual(inspected, [b""])
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.keys.glob(".mjobs-config-*.tmp")), [])

    def test_sync_failure_leaves_no_partial_key(self):
        destination = self.keys / "unsynced.key"
        with patch.object(backup.os, "fsync", side_effect=OSError(CANARY)), self.assertRaises(backup.ProtectedConfigError):
            backup.generate_key(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.keys.glob(".mjobs-config-*.tmp")), [])

    def test_linked_input_and_linked_ancestor_are_rejected(self):
        env = self.source / ".env"
        env.unlink()
        try:
            env.symlink_to(self.key)
        except OSError as error:
            self.skipTest(f"Synthetic symlink fixture unavailable: {type(error).__name__}")
        with self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()
        env.unlink()
        self.write_private(env, FILES[".env"])
        linked = self.root / "linked-source"
        linked.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(backup.ProtectedConfigError):
            backup.backup_config(linked, [".env"], self.key, self.archive)
        self.assertFalse(self.archive.exists())

    def test_hardlinked_source_or_key_is_rejected(self):
        alias = self.source / "alias"
        for path in (self.source / ".env", self.key):
            with self.subTest(path=path.name):
                os.link(path, alias)
                try:
                    with self.assertRaises(backup.ProtectedConfigError):
                        self.create_archive()
                finally:
                    alias.unlink()
        self.assertFalse(self.archive.exists())

    def test_oversized_file_and_key_are_rejected(self):
        with (self.source / ".env").open("wb") as stream:
            stream.truncate(backup.MAX_FILE_BYTES + 1)
        with self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()
        (self.source / ".env").write_bytes(FILES[".env"])
        self.key.write_bytes(b"x" * 33)
        with self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()
        self.assertFalse(self.archive.exists())

    def test_source_change_during_read_or_later_capture_is_rejected(self):
        env = self.source / ".env"
        real_fstat = os.fstat
        count = 0

        def changing_stat(descriptor):
            nonlocal count
            count += 1
            if count == 2:
                env.write_bytes(b"CHANGED_DURING_READ=" + CANARY.encode())
            return real_fstat(descriptor)

        with patch.object(backup.os, "fstat", side_effect=changing_stat), self.assertRaises(backup.ProtectedConfigError):
            backup._read_private(env, backup.MAX_FILE_BYTES)
        env.write_bytes(FILES[".env"])
        read = backup._read_snapshot

        def changing_capture(path, limit):
            result = read(path, limit)
            if path.name == "compose.private.yml":
                env.write_bytes(b"CHANGED_AFTER_READ=" + CANARY.encode())
            return result

        with patch.object(backup, "_read_snapshot", side_effect=changing_capture), self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()
        self.assertFalse(self.archive.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX filesystem permission fixture")
    def test_posix_broad_file_and_parent_modes_and_fifo_are_rejected(self):
        env = self.source / ".env"
        env.chmod(0o644)
        with self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()
        env.chmod(0o600)
        self.keys.chmod(0o755)
        with self.assertRaises(backup.ProtectedConfigError):
            backup.generate_key(self.keys / "new.key")
        self.keys.chmod(0o700)
        env.unlink()
        os.mkfifo(env, 0o600)
        with self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()

    @unittest.skipUnless(os.name == "nt", "Native NTFS leaf DACL fixture")
    def test_ntfs_broad_leaf_read_acl_is_rejected_even_under_private_parent(self):
        env = self.source / ".env"
        script = r'''$ErrorActionPreference='Stop';
$p=$env:MJOBS_SYNTHETIC_ACL_FILE; $a=Get-Acl -LiteralPath $p;
$s=[Security.Principal.SecurityIdentifier]::new('S-1-1-0');
$r=[Security.AccessControl.FileSystemAccessRule]::new($s,'Read','Allow');
$a.AddAccessRule($r); Set-Acl -LiteralPath $p -AclObject $a'''
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")}
        environment["MJOBS_SYNTHETIC_ACL_FILE"] = str(env)
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=environment, check=True, capture_output=True, timeout=10)
        backup._private_parent(self.source)
        with self.assertRaises(backup.ProtectedConfigError):
            self.create_archive()
        self.assertFalse(self.archive.exists())

    @unittest.skipUnless(os.name == "nt", "Native NTFS junction fixture")
    def test_ntfs_junction_ancestor_is_rejected(self):
        linked = self.root / "junction-source"
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")}
        environment.update(MJOBS_SYNTHETIC_JUNCTION=str(linked), MJOBS_SYNTHETIC_TARGET=str(self.source))
        script = r'''$ErrorActionPreference='Stop';
New-Item -ItemType Junction -Path $env:MJOBS_SYNTHETIC_JUNCTION -Target $env:MJOBS_SYNTHETIC_TARGET | Out-Null'''
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=environment, check=True, capture_output=True, timeout=10)
        try:
            with self.assertRaises(backup.ProtectedConfigError):
                backup.backup_config(linked, [".env"], self.key, self.archive)
        finally:
            linked.rmdir()
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
