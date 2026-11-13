"""Portable refusal checks for the Linux browser-timeout qualification fixture."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.integration import rq_workhorse_fixture as fixture


class BrowserTimeoutFixtureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.profile = self.root / "application" / "browser-profile"
        self.profile.mkdir(parents=True)
        self.worker = fixture.OwnedWorker(None, None, None, None, self.root, "unit")
        self.worker.process = SimpleNamespace(poll=lambda: None)
        self.worker.identity = fixture.ProcessIdentity(100, 1, 100, 100, 10)
        self.worker.horse = fixture.ProcessIdentity(101, 100, 101, 100, 11)
        self.driver = fixture.ProcessIdentity(102, 101, 101, 100, 12)
        self.browser = fixture.ProcessIdentity(103, 102, 103, 103, 13)
        self.renderer = fixture.ProcessIdentity(104, 103, 103, 103, 14)
        self.arguments = ("headless_shell", f"--user-data-dir={self.profile.resolve()}",
                          "--remote-debugging-pipe")

    def select(self, children, metadata, freeze):
        with patch.object(fixture, "running", return_value=True), patch.object(
                fixture, "descendants", return_value=children), patch.object(
                fixture, "process_command", side_effect=lambda owned: metadata[owned.pid]), patch.object(
                fixture, "freeze_process", freeze):
            return self.worker.freeze_browser(self.profile)

    def test_only_exact_owned_detached_browser_root_is_frozen(self):
        freeze = Mock(return_value=True)
        metadata = {102: ("node", ("node", "driver")),
                    103: ("headless_shell", self.arguments),
                    104: ("headless_shell", (*self.arguments, "--type=renderer"))}
        selected = self.select([self.driver, self.browser, self.renderer], metadata, freeze)
        self.assertEqual(selected, self.browser)
        freeze.assert_called_once_with(self.browser)
        self.assertEqual(set(self.worker.owned), {(101, 11), (102, 12), (103, 13), (104, 14)})

    def test_ambiguous_or_inexact_metadata_never_signals(self):
        second = fixture.ProcessIdentity(105, 102, 105, 105, 15)
        cases = [
            ([self.browser, second], {103: ("headless_shell", self.arguments),
                                     105: ("headless_shell", self.arguments)}),
            ([self.browser], {103: ("node", self.arguments)}),
            ([self.browser], {103: ("headless_shell", ("headless_shell", "--user-data-dir=/other",
                                                       "--remote-debugging-pipe"))}),
            ([self.browser], {103: ("headless_shell", (*self.arguments, "--type=renderer"))}),
            ([self.browser], {103: ("headless_shell", (*self.arguments, self.arguments[1]))}),
            ([self.browser], {103: ("headless_shell", self.arguments[:-1])}),
            ([self.browser], {103: ("headless_shell", (*self.arguments, "--user-data-dir", "/other"))}),
            ([self.browser], {103: ("headless_shell", (*self.arguments, "--remote-debugging-pipe=other"))}),
        ]
        for children, metadata in cases:
            with self.subTest(children=children, metadata=metadata), self.assertRaisesRegex(
                    RuntimeError, "one exact owned Chromium"):
                freeze = Mock()
                try:
                    self.select(children, metadata, freeze)
                finally:
                    freeze.assert_not_called()

    def test_shared_group_or_dead_browser_cannot_qualify_the_fault(self):
        shared = replace(self.browser, group=101, session=100)
        freeze = Mock(return_value=True)
        with self.assertRaisesRegex(RuntimeError, "detached session"):
            self.select([shared], {103: ("headless_shell", self.arguments)}, freeze)
        freeze.assert_not_called()
        freeze.return_value = False
        with self.assertRaisesRegex(RuntimeError, "fault was established"):
            self.select([self.browser], {103: ("headless_shell", self.arguments)}, freeze)
        freeze.assert_called_once_with(self.browser)

    def test_profile_outside_fixture_is_refused_before_process_inventory(self):
        with patch.object(fixture, "running", return_value=True), patch.object(
                fixture, "descendants") as inventory, patch.object(fixture, "freeze_process") as freeze:
            with self.assertRaisesRegex(RuntimeError, "outside the owned"):
                self.worker.freeze_browser(self.root.parent)
        inventory.assert_not_called()
        freeze.assert_not_called()

    def test_metadata_read_refuses_pid_reuse_before_or_after_read(self):
        process = self.root / str(self.browser.pid)
        process.mkdir()
        (process / "cmdline").write_bytes(b"headless_shell\0--remote-debugging-pipe\0")
        reused = replace(self.browser, started=999)
        for sequence in ([reused], [self.browser, reused]):
            with self.subTest(sequence=sequence), patch.object(
                    fixture, "process_identity", side_effect=sequence), patch.object(
                    fixture.os, "readlink", return_value="/owned/headless_shell"), self.assertRaisesRegex(
                    RuntimeError, "identity changed"):
                fixture.process_command(self.browser, proc=self.root)

    def test_snapshot_counts_a_stopped_original_but_never_a_reused_pid(self):
        with patch.object(fixture, "process_identity", return_value=replace(self.browser, state="T")):
            self.assertTrue(fixture.process_snapshot(self.browser)["alive"])
        with patch.object(fixture, "process_identity", return_value=replace(self.browser, started=999)):
            result = fixture.process_snapshot(self.browser)
            self.assertFalse(result["alive"])
            self.assertEqual(result["state"], "PID_REUSED")
