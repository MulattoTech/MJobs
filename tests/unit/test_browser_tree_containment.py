"""Native launch containment, never root-only or missing-handle exit proof."""
import importlib
import json
import os
from pathlib import Path
import subprocess
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

    def test_host_environment_is_minimal_case_insensitive_and_unicode(self):
        values = {"SystemRoot": r"C:\Windows", "TEMP": r"C:\synthetic\é",
                  "localappdata": r"C:\synthetic\cache", "PATH": "untrusted-path",
                  "PYTHONPATH": "untrusted-import", "HTTP_PROXY": "synthetic-proxy",
                  "DATABASE_URL": "synthetic-db", "MJobs_Unknown_Secret": "synthetic-secret"}
        with patch.dict(os.environ, values, clear=True):
            environment = self.module._host_environment()
        self.assertEqual(environment, {"SYSTEMROOT": values["SystemRoot"],
                                     "TEMP": values["TEMP"],
                                     "LOCALAPPDATA": values["localappdata"]})

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_native_host_does_not_inherit_synthetic_api_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "environment.json"
            secrets = ["MJOBS_SYNTHETIC_SECRET", "DATABASE_URL", "PYTHONPATH", "HTTP_PROXY"]
            keys = [*secrets, "TEMP"]
            temporary_path = str(Path(directory) / "synthetic-é")
            script = ("import json,os,sys; from pathlib import Path; "
                      "Path(sys.argv[1]).write_text(json.dumps({k:os.environ.get(k) "
                      "for k in sys.argv[2:]}))")
            job = self.module.BrowserTreeJob()
            query = job.query_handle()
            try:
                with patch.dict(os.environ, {**dict.fromkeys(secrets, "synthetic-only"),
                                             "TEMP": temporary_path}):
                    job.launch([sys.executable, "-c", script, str(output), *keys])
                eventually(output.exists)
                eventually(lambda: query.status() == "EXITED")
                self.assertEqual(json.loads(output.read_text()),
                                 {**dict.fromkeys(secrets), "TEMP": temporary_path})
                self.assertEqual(query.accounting()["terminated"], 0)
            finally:
                query.close()
                job.close()

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

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_query_transfer_rejects_missing_target_and_closed_source(self):
        job = self.module.BrowserTreeJob()
        query = job.query_handle()
        try:
            for target in (None, True, 0, -1, "1"):
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ValueError, "TREE_QUERY_TRANSFER_UNAVAILABLE"):
                        query.duplicate_to(target)
            with patch.object(query._kernel, "DuplicateHandle", return_value=False):
                with self.assertRaisesRegex(ValueError, "TREE_QUERY_TRANSFER_UNAVAILABLE"):
                    query.duplicate_to(123)
            self.assertEqual(query.status(), "UNKNOWN")
            query.close()
            with self.assertRaisesRegex(ValueError, "TREE_QUERY_TRANSFER_UNAVAILABLE"):
                query.duplicate_to(123)
        finally:
            query.close()
            job.close()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_external_observer_survives_creator_exit_with_live_orphan(self):
        self._external_observer("root")

    @unittest.skipUnless(sys.platform == "win32", "Native Windows containment")
    def test_external_observer_survives_creator_exit_with_actual_chromium(self):
        self._external_observer("browser")

    def _external_observer(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            creator = subprocess.Popen(
                [sys.executable, str(Path("tests/fixtures/browser_tree_observer.py").resolve()),
                 "creator", directory, scenario], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            ready = observer = None
            try:
                eventually(lambda: (location / "creator.json").exists(), 15)
                identity = json.loads((location / "creator.json").read_text())
                eventually(lambda: owner_process_status(identity) == "EXITED")
                eventually(lambda: (location / "ready.json").exists(), 20)
                ready = json.loads((location / "ready.json").read_text())
                observer = json.loads((location / "observer-ready.json").read_text())
                self.assertEqual(owner_process_status(observer["identity"]), "LIVE")
                self.assertIs(observer["contained"], False)
                self.assertEqual(observer["limits"], 0)
                self.assertIs(observer["setAllowed"], False)
                self.assertEqual(observer["setError"], 5)  # ERROR_ACCESS_DENIED
                eventually(lambda: json.loads((location / "observation.json").read_text())["status"] == "LIVE")
                if scenario == "root":
                    eventually(lambda: owner_process_status(ready["host"]) == "EXITED")
                    self.assertEqual(owner_process_status(ready["descendant"]), "LIVE")
                else:
                    self.assertGreaterEqual(len(ready["members"]), 3)
                (location / "release").touch()
                eventually(lambda: json.loads((location / "observation.json").read_text())["status"] == "EXITED", 20)
                value = json.loads((location / "observation.json").read_text())
                self.assertEqual(value["accounting"]["active"], 0)
                self.assertEqual(value["accounting"]["terminated"], 0)
                eventually(lambda: owner_process_status(observer["identity"]) == "EXITED")
                self.assertEqual(creator.wait(timeout=10), 0)
            finally:
                (location / "release").touch()
                (location / "observer-release").touch()
                creator.wait(timeout=15)
                # Startup failure can precede capturing either identity. Read
                # each independently after creator exit and cooperative release.
                if not ready and (location / "ready.json").exists():
                    ready = json.loads((location / "ready.json").read_text())
                if not observer and (location / "observer-ready.json").exists():
                    observer = json.loads((location / "observer-ready.json").read_text())
                if ready:
                    eventually(lambda: owner_process_status(ready.get("descendant", ready["host"])) == "EXITED", 30)
                elif (location / "descendant.json").exists():
                    descendant = json.loads((location / "descendant.json").read_text())
                    eventually(lambda: owner_process_status(descendant) == "EXITED", 30)
                if observer:
                    eventually(lambda: owner_process_status(observer["identity"]) == "EXITED", 15)

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
