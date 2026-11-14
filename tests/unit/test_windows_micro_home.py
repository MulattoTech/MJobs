"""Explicit Windows deployment-root checks; no real updater, Docker or network."""
import ctypes
import io
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from infra.deploy.windows import update_mjobs_micro as cd


WINDOWS_HOME = r"C:\MJobsPrivate"
OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
PRIVATE = "SYNTHETIC_PRIVATE_CONFIGURATION_VALUE"


class ExplicitHomePathTests(unittest.TestCase):
    def test_literal_drive_paths_preserve_the_intended_location(self):
        for value in (WINDOWS_HOME, "D:/MJobs Private", "c:\\MJobsPrivate\\",
                      "C:\\MJobs-\N{SNOWMAN}"):
            with self.subTest(value=value):
                self.assertEqual(cd._explicit_home_path(value), PureWindowsPath(value))

    def test_invalid_paths_refuse_without_expansion_or_filesystem_access(self):
        values = (
            "", " ", None, 12, "relative", r"C:relative", r"\root-relative",
            r"\\server\share\MJobs", r"\\?\C:\MJobs", r"\\.\C:\MJobs",
            "C:\\", r"C:\home\..\other", r"C:\home\.\other",
            r"C:\home\\other", r"C:\home\%USERNAME%", r"C:\home\$env:USER",
            r"C:\home:stream", r"C:\home\*", r"C:\home\?", r'C:\home\"quoted',
            r"C:\home\CON", r"C:\home\aux.txt", r"C:\home\LPT1",
            "C:\\home\\trailing.", "C:\\home\\trailing ", " C:\\home",
            "C:\\home\nprivate", "C:\\home\0private", "C:\\" + "x" * 32767,
        )
        with patch.object(cd, "require_plain_path") as plain, \
                patch.object(cd, "_require_local_drive") as local:
            for value in values:
                with self.subTest(value=repr(value)[:80]):
                    with self.assertRaisesRegex(RuntimeError, "^MJOBS_MICRO_HOME_INVALID$"):
                        cd._explicit_home_path(value)
            plain.assert_not_called()
            local.assert_not_called()

    def test_only_fixed_local_drive_type_is_accepted(self):
        get_type = Mock(return_value=3)
        kernel = SimpleNamespace(GetDriveTypeW=get_type)
        loader = Mock(return_value=kernel)
        with patch.object(ctypes, "WinDLL", loader, create=True):
            cd._require_local_drive("C:\\")
            loader.assert_called_once_with("kernel32", use_last_error=True)
            get_type.assert_called_once_with("C:\\")
            for kind in (0, 1, 2, 4, 5, 6):
                with self.subTest(kind=kind):
                    get_type.return_value = kind
                    with self.assertRaisesRegex(RuntimeError, "^MJOBS_MICRO_HOME_INVALID$"):
                        cd._require_local_drive("Z:\\")


class PrivateHomeFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "private-home"
        (self.home / "config").mkdir(parents=True)
        self.legacy = self.root / "legacy-home"
        self.legacy_base = self.root / "legacy-base"
        self.legacy_base.mkdir()
        self.patched = {
            "HOME": self.legacy, "BASE": self.legacy_base,
            "STATE": self.legacy / "state.json", "LOG": self.legacy / "update.log",
        }
        paths = patch.multiple(cd, **self.patched)
        paths.start()
        self.addCleanup(paths.stop)
        environment = dict(os.environ)
        environment.pop("MJOBS_MICRO_HOME", None)
        env = patch.dict(os.environ, environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        # Exercise real temporary filesystem layout checks on every platform.
        # Only the Windows-literal-to-native-path boundary is mapped.
        def native_path(value="."):
            return self.home if str(value) == WINDOWS_HOME else Path(value)
        constructor = patch.object(cd, "Path", side_effect=native_path)
        constructor.start()
        self.addCleanup(constructor.stop)
        drive = patch.object(cd, "_require_local_drive")
        self.drive = drive.start()
        self.addCleanup(drive.stop)

    def activate(self):
        self.assertTrue(cd.configure_deployment_home({"MJOBS_MICRO_HOME": WINDOWS_HOME}))

    def main_as_windows(self, locking, **kwargs):
        return patch.multiple(
            cd, os=SimpleNamespace(name="nt", SEEK_END=os.SEEK_END),
            msvcrt=SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking), **kwargs)

    def baseline(self):
        self.activate()
        old = self.home / "releases" / OLD_SHA
        new = self.home / "releases" / NEW_SHA
        old.mkdir(parents=True)
        new.mkdir()
        (new / "compose.yml").write_text("MJOBS_IMAGE MJOBS_PUBLIC_HOST\n", encoding="utf-8")
        for name in (".env", "compose.micro.yml"):
            (self.home / "config" / name).write_text(PRIVATE + "\n", encoding="utf-8")
            (old / name).write_text(PRIVATE + "\n", encoding="utf-8")
            (self.legacy_base / name).write_text("LEGACY_MUST_NOT_BE_COPIED\n", encoding="utf-8")
        cd.STATE.write_text(json.dumps({
            "sha": OLD_SHA, "image": "mjobs-micro:sha-" + OLD_SHA,
            "source": str(old), "deployed_at": "2026-11-01T00:00:00Z",
        }), encoding="utf-8")
        executable = self.root / "synthetic-python.exe"
        executable.touch()
        runtime = patch.object(cd, "OPS_PYTHON", executable)
        runtime.start()
        self.addCleanup(runtime.stop)
        return old, new


class HomeConfigurationTests(PrivateHomeFixture):
    def test_unset_keeps_all_legacy_or_injected_paths_untouched(self):
        with patch.object(cd, "require_plain_path") as plain:
            self.assertFalse(cd.configure_deployment_home({}))
        self.assertEqual({name: getattr(cd, name) for name in self.patched}, self.patched)
        self.drive.assert_not_called()
        plain.assert_not_called()
        self.assertFalse(self.legacy.exists())

    def test_explicit_setting_maps_all_paths_without_creating_other_entries(self):
        self.activate()
        self.assertEqual(cd.HOME, self.home)
        self.assertEqual(cd.BASE, self.home / "config")
        self.assertEqual(cd.STATE, self.home / "state.json")
        self.assertEqual(cd.LOG, self.home / "update.log")
        self.drive.assert_called_once_with("C:\\")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["config"])
        self.assertFalse(self.legacy.exists())

    def test_empty_or_invalid_explicit_setting_cannot_write_or_fall_back(self):
        for value in ("", r"C:\home\..\other", PRIVATE):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {"MJOBS_MICRO_HOME": value}), \
                    patch.object(Path, "mkdir") as mkdir, patch.object(Path, "open") as opened, \
                    patch.object(cd, "log") as log, patch.object(cd, "command") as command, \
                    patch.object(cd, "run_update") as update, \
                    patch.object(cd.sys, "stderr", io.StringIO()) as stderr:
                self.assertEqual(cd.main(), 1)
                self.assertEqual(stderr.getvalue(), "BLOCKED: MJOBS_MICRO_HOME_INVALID\n")
                self.assertNotIn(PRIVATE, stderr.getvalue())
                for effect in (mkdir, opened, log, command, update):
                    effect.assert_not_called()
        self.assertEqual({name: getattr(cd, name) for name in self.patched}, self.patched)
        self.drive.assert_not_called()

    def test_invalid_override_after_a_valid_selection_does_not_use_either_root(self):
        self.activate()
        with patch.dict(os.environ, {"MJOBS_MICRO_HOME": ""}), \
                patch.object(cd, "run_update") as update, patch.object(cd, "log") as log, \
                patch.object(Path, "open") as opened, \
                patch.object(cd.sys, "stderr", io.StringIO()):
            self.assertEqual(cd.main(), 1)
            update.assert_not_called()
            log.assert_not_called()
            opened.assert_not_called()
        self.assertEqual(cd.HOME, self.home)
        self.assertFalse(self.legacy.exists())

    def test_existing_root_and_config_directories_are_required_without_creation(self):
        (self.home / "config").rmdir()
        with patch.object(Path, "mkdir") as mkdir:
            with self.assertRaisesRegex(RuntimeError, "^MJOBS_MICRO_HOME_INVALID$"):
                cd.configure_deployment_home({"MJOBS_MICRO_HOME": WINDOWS_HOME})
            mkdir.assert_not_called()
        self.home.rmdir()
        with self.assertRaisesRegex(RuntimeError, "^MJOBS_MICRO_HOME_INVALID$"):
            cd.configure_deployment_home({"MJOBS_MICRO_HOME": WINDOWS_HOME})
        self.assertFalse(self.home.exists())
        self.assertEqual(cd.HOME, self.legacy)

    def test_wrong_existing_entry_types_are_rejected_and_preserved(self):
        for relative, directory in (
            ("releases", False), ("backups", False), ("state.json", True),
            ("state.new.json", True), ("update.lock", True), ("update.log", True),
            ("update.old.log", True), ("update.py", True),
            ("config/.env", True), ("config/compose.micro.yml", True),
        ):
            with self.subTest(relative=relative):
                entry = self.home / relative
                if directory:
                    entry.mkdir()
                else:
                    entry.write_text("preserve", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "^MJOBS_MICRO_HOME_INVALID$"):
                    cd.configure_deployment_home({"MJOBS_MICRO_HOME": WINDOWS_HOME})
                self.assertTrue(entry.exists())
                self.assertEqual(cd.HOME, self.legacy)
                if directory:
                    entry.rmdir()
                else:
                    self.assertEqual(entry.read_text(encoding="utf-8"), "preserve")
                    entry.unlink()

    def test_reparse_ancestor_or_known_entry_refuses_without_touching_its_target(self):
        actual_lstat = Path.lstat
        for redirected in (self.root, self.home, self.home / "config",
                           self.home / "update.lock"):
            with self.subTest(redirected=redirected.name):
                def lstat(path, *args, **kwargs):
                    if path == redirected:
                        return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                    return actual_lstat(path, *args, **kwargs)
                with patch.object(Path, "lstat", lstat):
                    with self.assertRaisesRegex(RuntimeError, "^MJOBS_MICRO_HOME_INVALID$"):
                        cd.configure_deployment_home({"MJOBS_MICRO_HOME": WINDOWS_HOME})
                self.assertEqual(cd.HOME, self.legacy)
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["config"])

    def test_filesystem_or_drive_error_is_content_free_before_paths_change(self):
        for location in ("_require_local_drive", "_require_home_layout"):
            with self.subTest(location=location), \
                    patch.object(cd, location, side_effect=OSError(PRIVATE)):
                with self.assertRaisesRegex(RuntimeError, "^MJOBS_MICRO_HOME_INVALID$") as caught:
                    cd.configure_deployment_home({"MJOBS_MICRO_HOME": WINDOWS_HOME})
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(PRIVATE, str(caught.exception))
                self.assertEqual(cd.HOME, self.legacy)


class ExplicitHomeLockTests(PrivateHomeFixture):
    def test_selected_lock_remains_held_across_update_and_is_released(self):
        events = []
        held = False

        def locking(fd, mode, length):
            nonlocal held
            self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
            self.assertEqual(length, 1)
            if mode == 1:
                self.assertFalse(held)
                held = True
                events.append("acquire")
            else:
                self.assertTrue(held)
                held = False
                events.append("release")

        def update(*, explicit_home):
            self.assertTrue(explicit_home)
            self.assertTrue(held)
            self.assertEqual(cd.HOME, self.home)
            self.assertEqual(cd.STATE, self.home / "state.json")
            events.append("update")

        with patch.dict(os.environ, {"MJOBS_MICRO_HOME": WINDOWS_HOME}), \
                self.main_as_windows(locking, run_update=update):
            self.assertEqual(cd.main(), 0)
        self.assertEqual(events, ["acquire", "update", "release"])
        self.assertEqual((self.home / "update.lock").read_bytes(), b"0")
        self.assertFalse(self.legacy.exists())

    def test_later_unset_invocation_restores_caller_paths_and_legacy_mode(self):
        observed = []

        def update(*, explicit_home):
            observed.append((explicit_home, cd.HOME, cd.BASE, cd.STATE, cd.LOG))

        with self.main_as_windows(Mock(), run_update=update):
            with patch.dict(os.environ, {"MJOBS_MICRO_HOME": WINDOWS_HOME}):
                self.assertEqual(cd.main(), 0)
            self.assertEqual({name: getattr(cd, name) for name in self.patched}, self.patched)
            self.assertEqual(cd.main(), 0)
        self.assertEqual(observed, [
            (True, self.home, self.home / "config",
             self.home / "state.json", self.home / "update.log"),
            (False, self.legacy, self.legacy_base,
             self.legacy / "state.json", self.legacy / "update.log"),
        ])
        self.assertEqual((self.home / "update.lock").read_bytes(), b"0")
        self.assertEqual((self.legacy / "update.lock").read_bytes(), b"0")

    def test_busy_selected_lock_defers_without_update_log_or_unlock(self):
        locking = Mock(side_effect=OSError("busy"))
        update, log = Mock(), Mock()
        with patch.dict(os.environ, {"MJOBS_MICRO_HOME": WINDOWS_HOME}), \
                self.main_as_windows(locking, run_update=update, log=log):
            self.assertEqual(cd.main(), 0)
        self.assertEqual(locking.call_count, 1)
        self.assertEqual(locking.call_args.args[1:], (1, 1))
        update.assert_not_called()
        log.assert_not_called()
        self.assertFalse(self.legacy.exists())

    def test_failure_unlocks_before_logging_only_in_selected_home(self):
        events = []

        def locking(_fd, mode, _length):
            events.append("acquire" if mode == 1 else "release")

        def log(message):
            self.assertEqual(events, ["acquire", "release"])
            self.assertEqual(cd.LOG, self.home / "update.log")
            self.assertEqual(message, "BLOCKED: FIXED_FAILURE")
            events.append("log")

        with patch.dict(os.environ, {"MJOBS_MICRO_HOME": WINDOWS_HOME}), \
                self.main_as_windows(
                    locking, run_update=Mock(side_effect=RuntimeError("FIXED_FAILURE")), log=log):
            self.assertEqual(cd.main(), 1)
        self.assertEqual(events, ["acquire", "release", "log"])
        self.assertFalse(self.legacy.exists())


class RelocatedBaselineTests(PrivateHomeFixture):
    def test_outside_old_source_is_rejected_before_git_github_or_migration(self):
        _old, _new = self.baseline()
        outside = self.root / "outside-release"
        outside.mkdir()
        previous = json.loads(cd.STATE.read_text(encoding="utf-8"))
        previous["source"] = str(outside)
        cd.STATE.write_text(json.dumps(previous), encoding="utf-8")
        with patch.object(cd, "verify_candidate") as verify, \
                patch.object(cd, "main_sha") as main, \
                patch.object(cd, "migration_digest") as digest, \
                patch.object(cd, "command") as command, patch.object(cd, "compose") as compose:
            with self.assertRaisesRegex(RuntimeError, "^Invalid relocated deployment source$"):
                cd.run_update(explicit_home=True)
            for effect in (verify, main, digest, command, compose):
                effect.assert_not_called()

    def test_old_clone_validation_precedes_even_an_unchanged_main_check(self):
        old, _new = self.baseline()
        with patch.object(cd, "verify_candidate", side_effect=RuntimeError("INVALID_OLD_CLONE")) as verify, \
                patch.object(cd, "main_sha", return_value=OLD_SHA) as main, \
                patch.object(cd, "migration_digest") as digest:
            with self.assertRaisesRegex(RuntimeError, "^INVALID_OLD_CLONE$"):
                cd.run_update(explicit_home=True)
            verify.assert_called_once_with(old, OLD_SHA)
            main.assert_not_called()
            digest.assert_not_called()

    def test_root_config_overlays_backup_and_new_state_stay_together(self):
        old, new = self.baseline()
        events = []
        commands = []

        def verified(source, sha):
            events.append(("verify", source, sha))

        def main():
            events.append(("github",))
            return NEW_SHA

        def command(argv, **kwargs):
            commands.append((argv, kwargs))
            self.assertTrue(argv[:2] == ["docker", "build"]
                            or argv[1:3] == ["-m", "cli.local_backup"])
            return ""

        with patch.object(cd, "verify_candidate", side_effect=verified), \
                patch.object(cd, "main_sha", side_effect=main), \
                patch.object(cd, "checks_pass", return_value=True) as checks, \
                patch.object(cd, "prepare_candidate", return_value=new), \
                patch.object(cd, "migration_digest", return_value="same"), \
                patch.object(cd, "command", side_effect=command), \
                patch.object(cd, "compose") as compose, patch.object(cd, "ready") as ready:
            cd.run_update(explicit_home=True)
        self.assertEqual(events[0], ("verify", old, OLD_SHA))
        self.assertEqual(events[1], ("github",))
        self.assertEqual(checks.call_count, 2)
        self.assertIn(("verify", new, NEW_SHA), events)
        for name in (".env", "compose.micro.yml"):
            self.assertEqual((new / name).read_bytes(), (self.home / "config" / name).read_bytes())
            self.assertEqual((self.legacy_base / name).read_text(encoding="utf-8"),
                             "LEGACY_MUST_NOT_BE_COPIED\n")
        backup = next((argv, kwargs) for argv, kwargs in commands if "backup" in argv)
        archive = Path(backup[0][backup[0].index("--archive") + 1])
        self.assertEqual(archive.parent, self.home / "backups")
        self.assertEqual(backup[1]["cwd"], old)
        self.assertTrue(backup[1]["backup_diagnostics"])
        self.assertEqual(compose.call_args_list[0].args[:4],
                         (old, OLD_SHA, "mjobs-micro:sha-" + OLD_SHA, "stop"))
        ready.assert_called_once_with(NEW_SHA)
        previous = json.loads(cd.STATE.read_text(encoding="utf-8"))
        self.assertEqual(previous["sha"], NEW_SHA)
        self.assertEqual(previous["source"], str(new))
        self.assertEqual(previous["image"], "mjobs-micro:sha-" + NEW_SHA)
        self.assertFalse((self.home / "state.new.json").exists())
        self.assertTrue(cd.LOG.is_file())
        self.assertFalse(self.legacy.exists())

    def test_explicit_root_still_refuses_changed_migrations_before_overlay_or_docker(self):
        old, new = self.baseline()
        with patch.object(cd, "verify_candidate") as verify, \
                patch.object(cd, "main_sha", return_value=NEW_SHA), \
                patch.object(cd, "checks_pass", return_value=True), \
                patch.object(cd, "prepare_candidate", return_value=new), \
                patch.object(cd, "migration_digest", side_effect=["new", "old"]), \
                patch.object(cd, "command") as command, patch.object(cd, "compose") as compose:
            with self.assertRaisesRegex(RuntimeError, "New migrations require offline guarded review"):
                cd.run_update(explicit_home=True)
            verify.assert_called_once_with(old, OLD_SHA)
            command.assert_not_called()
            compose.assert_not_called()
        self.assertFalse((new / ".env").exists())
        self.assertEqual(json.loads(cd.STATE.read_text(encoding="utf-8"))["sha"], OLD_SHA)


if __name__ == "__main__":
    unittest.main()
