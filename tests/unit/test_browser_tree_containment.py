"""Native launch containment, never root-only or missing-handle exit proof."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from services.applications.browser_process import owner_process_status


def eventually(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("Bounded contained-child observation timed out")


class BrowserTreeContainmentTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("services.applications.browser_tree")
        except ModuleNotFoundError:
            self.fail("Atomic browser-host containment is missing")

    def test_unsupported_runtime_fails_closed(self):
        with patch.object(self.module.sys, "platform", "unsupported"):
            with self.assertRaisesRegex(ValueError, "TREE_CONTAINMENT_UNAVAILABLE"):
                self.module.BrowserTreeJob()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_unlaunched_job_and_lost_query_never_prove_exit(self):
        job = self.module.BrowserTreeJob()
        query = job.query_handle()
        try:
            self.assertEqual(query.status(), "UNKNOWN")
            with patch.object(query._kernel, "QueryInformationJobObject", return_value=False):
                self.assertEqual(query.status(), "UNKNOWN")
                self.assertIsNone(query.limit_flags())
            self.assertIsNone(query.contains(True))
            self.assertIsNone(query.contains(-1))
            query.close()
            query.close()
            self.assertEqual(query.status(), "UNKNOWN")
            self.assertIsNone(query.contains(os.getpid()))
        finally:
            job.close()
            job.close()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_failed_launch_cannot_be_retried_or_reported_exited(self):
        job = self.module.BrowserTreeJob()
        query = job.query_handle()
        try:
            with self.assertRaisesRegex(ValueError, "TREE_LAUNCH_UNAVAILABLE"):
                job.launch([str(Path.cwd() / "missing-native-browser-host.exe")])
            self.assertEqual(query.status(), "UNKNOWN")
            with self.assertRaisesRegex(ValueError, "TREE_LAUNCH_UNAVAILABLE"):
                job.launch([sys.executable, "-c", "pass"])
        finally:
            query.close()
            job.close()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_root_exit_orphan_live_and_creator_close_retains_tree_proof(self):
        self._native_scenario("root")

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_actual_chromium_members_are_contained_until_cooperative_close(self):
        self._native_scenario("browser")

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_last_handle_loss_never_reports_orphan_exit(self):
        self._native_scenario("root", lose_query=True)

    def _native_scenario(self, role, lose_query=False):
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            job = self.module.BrowserTreeJob()
            query = job.query_handle()
            ready = None
            try:
                pid = job.launch([sys.executable, str(Path("tests/fixtures/browser_tree_child.py").resolve()),
                                  role, directory], cwd=Path.cwd())
                self.assertGreater(pid, 0)
                eventually(lambda: (location / "ready.json").exists(), 20)
                ready = json.loads((location / "ready.json").read_text())
                self.assertIs(query.contains(os.getpid()), False)
                self.assertEqual(query.status(), "LIVE")
                self.assertEqual(query.limit_flags(), 0)
                if role == "root":
                    eventually(lambda: owner_process_status(ready["host"]) == "EXITED")
                    self.assertEqual(owner_process_status(ready["descendant"]), "LIVE")
                    self.assertTrue(query.contains(ready["descendant"]["pid"]))
                else:
                    self.assertGreaterEqual(len(ready["members"]), 3)
                    for pid in ready["members"]:
                        self.assertTrue(query.contains(pid))
                # Creator loss must neither kill the orphan nor destroy proof
                # while an independently retained query handle still exists.
                job.close()
                self.assertEqual(query.status(), "LIVE")
                if lose_query:
                    query.close()
                    self.assertEqual(query.status(), "UNKNOWN")
                    self.assertEqual(owner_process_status(ready["descendant"]), "LIVE")
                (location / "release").touch()
                if lose_query:
                    eventually(lambda: owner_process_status(ready["descendant"]) == "EXITED")
                    self.assertEqual(query.status(), "UNKNOWN")
                else:
                    eventually(lambda: query.status() == "EXITED", 20)
                    self.assertEqual(query.accounting()["active"], 0)
                    self.assertEqual(query.accounting()["terminated"], 0)
                    with self.assertRaisesRegex(ValueError, "TREE_LAUNCH_UNAVAILABLE"):
                        job.launch([sys.executable, "-c", "pass"])
            finally:
                (location / "release").touch()
                if ready:
                    identity = ready.get("descendant", ready["host"])
                    eventually(lambda: owner_process_status(identity) == "EXITED", 30)
                query.close()
                job.close()
