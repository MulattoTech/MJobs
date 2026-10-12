"""Private observer channel: exact scope, bounded acknowledgement, no release."""
import importlib
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from services.applications.browser_tree import BrowserTreeJob
from tests.unit.test_browser_tree_containment import eventually


def binding():
    return {"operation_id": "synthetic-operation", "application_id": "synthetic-app",
            "attempt_id": "synthetic-attempt", "profile_id": "synthetic-profile",
            "owner_digest": "a" * 64}


class BrowserTreeIPCTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module("services.applications.browser_tree_ipc")
        except ModuleNotFoundError:
            self.fail("Private bound observer IPC is missing")

    def test_scope_validation_rejects_unknown_missing_and_oversized_fields(self):
        module = self.module()
        for value in (None, {}, {**binding(), "extra": "private"},
                      {**binding(), "owner_digest": "x" * 64},
                      {**binding(), "operation_id": "x" * 257},
                      {**binding(), "operation_id": "é" * 256},
                      {**binding(), "operation_id": '"' * 256},
                      {**binding(), "operation_id": "\\" * 256},
                      {**binding(), "profile_id": True}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "TREE_OBSERVER_UNAVAILABLE"):
                    module.validate_binding(value)

    def test_oversized_send_is_rejected_before_touching_pipe(self):
        from unittest.mock import Mock

        module = self.module()
        stream = Mock()
        with self.assertRaisesRegex(ValueError, "TREE_OBSERVER_UNAVAILABLE"):
            module._write(stream, {"nonce": "x" * 2048})
        stream.write.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows maximum bound frame")
    def test_maximum_ascii_scope_roundtrips_within_pipe_budget(self):
        module = self.module()
        scope = {key: ("a" * 64 if key == "owner_digest" else "x" * 256)
                 for key in binding()}
        job = BrowserTreeJob()
        query = job.query_handle()
        observer = None
        try:
            observer = module.BrowserTreeObserver(query, scope)
            self.assertEqual(observer.observe()["binding"], scope)
        finally:
            if observer is not None:
                observer.close()
            query.close()
            job.close()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows observer IPC")
    def test_private_native_channel_bound_to_query_and_exact_scope(self):
        module = self.module()
        job = BrowserTreeJob()
        query = job.query_handle()
        observer = None
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "close"
            try:
                with patch.dict(os.environ, {"MJOBS_SYNTHETIC_SECRET": "synthetic"}):
                    observer = module.BrowserTreeObserver(query, binding())
                self.assertNotEqual(observer.identity["pid"], os.getpid())
                self.assertEqual(observer.observe()["status"], "UNKNOWN")
                with self.assertRaisesRegex(ValueError, "TREE_OBSERVER_UNAVAILABLE"):
                    observer.observe({**binding(), "attempt_id": "stale-attempt"})
                job.launch([sys.executable, "-c",
                            "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                            "deadline=time.monotonic()+20; "
                            "\nwhile not p.exists() and time.monotonic()<deadline: time.sleep(.05)", str(output)])
                live = eventually(lambda: (v if (v := observer.observe())["status"] == "LIVE" else None))
                self.assertGreater(live["accounting"]["active"], 0)
                job.close()
                query.close()
                self.assertEqual(observer.observe()["status"], "LIVE")
                output.touch()
                closed = eventually(lambda: (v if (v := observer.observe())["status"] == "EXITED" else None))
                self.assertEqual(closed["accounting"]["active"], 0)
                self.assertEqual(closed["accounting"]["terminated"], 0)
                self.assertEqual(closed["binding"], binding())
                observer.close()
                self.assertEqual(observer.observe()["status"], "UNKNOWN")
            finally:
                output.touch()
                if observer is not None:
                    observer.close()
                query.close()
                job.close()

    def test_reply_rejects_wrong_challenge_scope_or_observer_identity(self):
        module = self.module()
        identity = {"pid": 123, "created": "123", "host": "a" * 64}
        expected = {"nonce": "a" * 64, "binding": binding(), "identity": identity,
                    "status": "UNKNOWN", "accounting": {"total": 0, "active": 0, "terminated": 0}}
        for value in ({**expected, "nonce": "b" * 64},
                      {**expected, "binding": {**binding(), "profile_id": "foreign"}},
                      {**expected, "identity": {**identity, "pid": 124}},
                      {**expected, "status": "EXITED"},
                      {**expected, "accounting": {"total": True, "active": 0, "terminated": 0}}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "TREE_OBSERVER_UNAVAILABLE"):
                    module.validate_reply(value, "a" * 64, binding(), identity)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows observer IPC")
    def test_missing_ack_fails_closed_and_owned_observer_exits(self):
        module = self.module()
        job = BrowserTreeJob()
        query = job.query_handle()
        processes = []
        original = module.subprocess.Popen

        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        try:
            with patch.object(module.subprocess, "Popen", side_effect=capture), \
                    patch.object(module, "_read", side_effect=TimeoutError("synthetic-private")):
                with self.assertRaisesRegex(ValueError, "^TREE_OBSERVER_UNAVAILABLE$"):
                    module.BrowserTreeObserver(query, binding())
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual(query.status(), "UNKNOWN")
        finally:
            query.close()
            job.close()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows bounded pipe reads")
    def test_partial_oversized_and_lost_pipe_fail_with_bounded_reads(self):
        module = self.module()
        # Use a smaller protocol cap for the oversized-frame control. Writing
        # 8192 bytes synchronously can fill a native pipe before its reader runs.
        for frame in (b"{", b"x" * 128, b"broken-json\n"):
            with self.subTest(frame_length=len(frame)):
                read, write = os.pipe()
                with os.fdopen(read, "rb", buffering=0) as source, \
                        os.fdopen(write, "wb", buffering=0) as sink:
                    sink.write(frame)
                    started = time.monotonic()
                    with patch.object(module, "_MAX_FRAME", 64):
                        with self.assertRaises(ValueError):
                            module._read(source, .1)
                    self.assertLess(time.monotonic() - started, 1)
        read, write = os.pipe()
        os.close(write)
        with os.fdopen(read, "rb", buffering=0) as source:
            with self.assertRaises(ValueError):
                module._read(source, .1)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows creator exit")
    def test_observer_retains_authority_after_disposable_creator_exit(self):
        from services.applications.browser_process import owner_process_status

        self.module()
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            script = ("import json,os,pathlib,sys; "
                      "from services.applications.browser_tree import BrowserTreeJob; "
                      "from services.applications.browser_tree_ipc import BrowserTreeObserver; "
                      "job=BrowserTreeJob(); query=job.query_handle(); "
                      "observer=BrowserTreeObserver(query,json.loads(sys.argv[2])); "
                      "job.launch([sys.executable,'-c','import pathlib,sys,time; "
                      "p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+20; "
                      "\\nwhile not p.exists() and time.monotonic()<deadline: time.sleep(.05)',"
                      "str(pathlib.Path(sys.argv[1])/'close')]); "
                      "pathlib.Path(sys.argv[1],'observer.json').write_text(json.dumps(observer.identity)); "
                      "os._exit(0)")
            creator = subprocess.Popen([sys.executable, "-c", script, directory, json.dumps(binding())],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            identity = None
            try:
                self.assertEqual(creator.wait(timeout=10), 0)
                identity = json.loads((location / "observer.json").read_text())
                # EOF was delivered by actual creator death. A dropped query
                # would make the observer exit immediately; it must stay alive.
                time.sleep(.2)
                self.assertEqual(owner_process_status(identity), "LIVE")
                (location / "close").touch()
                eventually(lambda: owner_process_status(identity) == "EXITED")
            finally:
                (location / "close").touch()
                if identity is not None:
                    eventually(lambda: owner_process_status(identity) == "EXITED", 25)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows actual Chromium channel")
    def test_actual_chromium_cooperative_close_via_bound_channel(self):
        module = self.module()
        job = BrowserTreeJob()
        query = job.query_handle()
        observer = None
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            try:
                observer = module.BrowserTreeObserver(query, binding())
                job.launch([sys.executable,
                            str(Path(__file__).resolve().parents[1] / "fixtures" / "browser_tree_child.py"),
                            "browser", directory], cwd=Path.cwd())
                eventually(lambda: (location / "ready.json").exists())
                ready = json.loads((location / "ready.json").read_text())
                self.assertGreaterEqual(len(ready["members"]), 2)
                for pid in ready["members"]:
                    self.assertTrue(query.contains(pid))
                job.close()
                query.close()
                self.assertEqual(observer.observe()["status"], "LIVE")
                (location / "release").touch()
                closed = eventually(lambda: (v if (v := observer.observe())["status"] == "EXITED" else None))
                self.assertEqual(closed["accounting"]["active"], 0)
                self.assertEqual(closed["accounting"]["terminated"], 0)
            finally:
                (location / "release").touch()
                if observer is not None:
                    observer.close()
                query.close()
                job.close()
