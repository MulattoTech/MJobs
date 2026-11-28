"""Browser session directories must stay private without repairing old profiles."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from services.applications.browser_profiles import browser_profile_path


class BrowserProfilePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def application(self, name="app"):
        root = self.root / name
        root.mkdir(mode=0o755)
        return root

    def pin(self, root, name):
        selection = root / "browser-profile-selection.json"
        selection.write_text(json.dumps({"schema_version": 1, "name": name}), encoding="utf-8")
        return selection

    @unittest.skipUnless(os.name == "posix", "POSIX directory-mode admission")
    def test_fresh_default_and_named_directories_are_private_under_open_umask(self):
        for label in ("default", "review"):
            with self.subTest(label=label):
                root = self.application(label)
                root.chmod(0o755)
                previous = os.umask(0)
                try:
                    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": label}):
                        profile = browser_profile_path(root, initialize=True)
                finally:
                    os.umask(previous)
                for directory in {root / "browser-profile", profile}:
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
                self.assertEqual(browser_profile_path(root), profile)

    @unittest.skipUnless(os.name == "posix", "POSIX directory-mode admission")
    def test_permissive_legacy_session_is_refused_without_mutation_or_selection(self):
        for mode in (0o755, 0o770, 0o701):
            for initialize in (False, True):
                with self.subTest(mode=oct(mode), initialize=initialize):
                    root = self.application(f"legacy-{mode}-{initialize}")
                    profile = root / "browser-profile"
                    profile.mkdir()
                    profile.chmod(mode)
                    cookie = profile / "Cookies"
                    cookie.write_bytes(b"synthetic-session-fixture")
                    with self.assertRaisesRegex(ValueError, "^BROWSER_PROFILE_UNSAFE$"):
                        browser_profile_path(root, initialize=initialize)
                    self.assertEqual(stat.S_IMODE(profile.stat().st_mode), mode)
                    self.assertEqual(cookie.read_bytes(), b"synthetic-session-fixture")
                    self.assertFalse((root / "browser-profile-selection.json").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX directory-mode admission")
    def test_pinned_session_checks_parent_and_selected_directory_without_repair(self):
        for public_component in ("parent", "selected"):
            for initialize in (False, True):
                with self.subTest(public_component=public_component, initialize=initialize):
                    root = self.application(f"{public_component}-{initialize}")
                    parent = root / "browser-profile"
                    parent.mkdir(mode=0o700)
                    profile = parent / "review"
                    profile.mkdir(mode=0o700)
                    exposed = parent if public_component == "parent" else profile
                    exposed.chmod(0o755)
                    cookie = profile / "Cookies"
                    cookie.write_bytes(b"synthetic-session-fixture")
                    selection = self.pin(root, "review")
                    pinned = selection.read_bytes()
                    with self.assertRaisesRegex(ValueError, "^BROWSER_PROFILE_UNSAFE$"):
                        browser_profile_path(root, initialize=initialize)
                    self.assertEqual(stat.S_IMODE(exposed.stat().st_mode), 0o755)
                    self.assertEqual(cookie.read_bytes(), b"synthetic-session-fixture")
                    self.assertEqual(selection.read_bytes(), pinned)

    def test_private_legacy_session_remains_pinned_after_environment_change(self):
        root = self.application()
        profile = root / "browser-profile"
        profile.mkdir(mode=0o700)
        cookie = profile / "Cookies"
        cookie.write_bytes(b"synthetic-session-fixture")
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "another"}):
            self.assertEqual(browser_profile_path(root, initialize=True), profile)
            self.assertEqual(browser_profile_path(root), profile)
        self.assertEqual(cookie.read_bytes(), b"synthetic-session-fixture")
        self.assertEqual(json.loads((root / "browser-profile-selection.json").read_text())["name"], "default")

    def test_profile_directory_must_not_be_a_regular_file(self):
        for named in (False, True):
            for initialize in (False, True):
                with self.subTest(named=named, initialize=initialize):
                    root = self.application(f"file-{named}-{initialize}")
                    parent = root / "browser-profile"
                    if named:
                        parent.mkdir(mode=0o700)
                        self.pin(root, "review")
                    target = parent / "review" if named else parent
                    target.write_bytes(b"synthetic-nondirectory")
                    with self.assertRaisesRegex(ValueError, "^BROWSER_PROFILE_UNSAFE$"):
                        browser_profile_path(root, initialize=initialize)
                    self.assertEqual(target.read_bytes(), b"synthetic-nondirectory")
                    if not named:
                        self.assertFalse((root / "browser-profile-selection.json").exists())

    def test_read_only_missing_profile_does_not_create_state(self):
        root = self.root / "missing"
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "invalid/ignored"}):
            self.assertEqual(browser_profile_path(root), root / "browser-profile")
        self.assertFalse(root.exists())

    def test_read_only_missing_pinned_profile_preserves_selection(self):
        root = self.application()
        selection = self.pin(root, "review")
        pinned = selection.read_bytes()
        self.assertEqual(browser_profile_path(root), root / "browser-profile" / "review")
        self.assertEqual(selection.read_bytes(), pinned)
        self.assertFalse((root / "browser-profile").exists())

    def test_creation_failure_is_sanitized(self):
        root = self.application()
        profile = root / "browser-profile"
        original = Path.mkdir

        def fail_profile(path, *args, **kwargs):
            if path == profile:
                raise PermissionError("synthetic-private-location")
            return original(path, *args, **kwargs)

        with patch.object(Path, "mkdir", fail_profile):
            with self.assertRaisesRegex(ValueError, "^BROWSER_PROFILE_UNSAFE$"):
                browser_profile_path(root, initialize=True)
        self.assertFalse(profile.exists())

    def test_stat_failure_is_sanitized(self):
        root = self.application()
        profile = root / "browser-profile"
        original = Path.lstat

        def fail_profile(path, *args, **kwargs):
            if path == profile:
                raise PermissionError("synthetic-private-location")
            return original(path, *args, **kwargs)

        with patch.object(Path, "lstat", fail_profile):
            with self.assertRaisesRegex(ValueError, "^BROWSER_PROFILE_UNSAFE$"):
                browser_profile_path(root, initialize=True)

    def test_replaced_directory_is_rejected_after_creation(self):
        root = self.application()
        profile = root / "browser-profile"
        original = Path.mkdir

        def replace_profile(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == profile:
                path.rmdir()
                path.write_bytes(b"synthetic-replacement")
            return result

        with patch.object(Path, "mkdir", replace_profile):
            with self.assertRaisesRegex(ValueError, "^BROWSER_PROFILE_UNSAFE$"):
                browser_profile_path(root, initialize=True)
        self.assertEqual(profile.read_bytes(), b"synthetic-replacement")

    def test_aliased_profile_is_rejected_before_pinning(self):
        root = self.application()
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        try:
            (root / "browser-profile").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks is unavailable on this host")
        for initialize in (False, True):
            with self.subTest(initialize=initialize):
                with self.assertRaisesRegex(ValueError, "^BROWSER_PROFILE_UNSAFE$"):
                    browser_profile_path(root, initialize=initialize)
        self.assertFalse((root / "browser-profile-selection.json").exists())
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
