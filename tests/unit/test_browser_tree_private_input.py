"""Private launch input must not expose bytes or inherit unrelated handles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from services.applications.browser_tree import BrowserTreeJob
from tests.unit.test_browser_tree_containment import eventually


@unittest.skipUnless(sys.platform == "win32", "Native Windows private host input")
class BrowserTreePrivateInputTests(unittest.TestCase):
    def test_concurrent_restricted_launcher_cannot_inherit_private_reader(self):
        import ctypes
        from ctypes import wintypes

        job = BrowserTreeJob()
        query = job.query_handle()
        kernel = job._kernel
        comparison = ctypes.WinDLL("kernelbase", use_last_error=True).CompareObjectHandles
        comparison.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        comparison.restype = wintypes.BOOL
        launch = job._launch
        observed = []

        def concurrent_launch(argv, cwd, handles):
            # Exercise another launcher while our reader is inheritable.
            # The project launch contract requires restricted inheritance.
            with subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(.5)"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True) as child:
                child_process = kernel.OpenProcess(0x40, False, child.pid)
                self.assertTrue(child_process)
                duplicate = wintypes.HANDLE()
                try:
                    copied = kernel.DuplicateHandle(child_process, handles[0],
                        kernel.GetCurrentProcess(), ctypes.byref(duplicate), 0, False, 2)
                    # Numeric handle reuse is harmless; compare kernel identity.
                    observed.append(bool(copied and comparison(
                        handles[0], duplicate)))
                finally:
                    if duplicate:
                        kernel.CloseHandle(duplicate)
                    kernel.CloseHandle(child_process)
                child.wait(timeout=5)
            return launch(argv, cwd, handles)

        try:
            with patch.object(job, "_launch", side_effect=concurrent_launch):
                job.launch([sys._base_executable, "-c", "import sys; sys.stdin.buffer.read()"],
                           private_input=b"synthetic-private-only")
            eventually(lambda: query.status() == "EXITED")
            self.assertEqual(observed, [False])
            self.assertEqual(query.accounting()["terminated"], 0)
        finally:
            query.close()
            job.close()

    def test_private_input_roundtrip_eof_and_unrelated_handle_exclusion(self):
        import msvcrt

        # Removing the explicit HANDLE_LIST or putting input on argv breaks
        # this child-observed oracle. All input and paths are synthetic.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "observed.json"
            unrelated_file = Path(directory) / "unrelated-api-file"
            unrelated_file.write_text("synthetic-only")
            unrelated_read = os.open(unrelated_file, os.O_RDONLY)
            unrelated_handle = msvcrt.get_osfhandle(unrelated_read)
            os.set_handle_inheritable(unrelated_handle, True)
            payload = b'{"profile":"synthetic-private-profile","nonce":"synthetic-only"}\n'
            script = (
                "import ctypes,json,sys; from ctypes import wintypes as w; "
                "from pathlib import Path; k=ctypes.WinDLL('kernel32',use_last_error=True); "
                "k.GetHandleInformation.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]; "
                "k.GetHandleInformation.restype=w.BOOL; flags=w.DWORD(); "
                "k.GetFinalPathNameByHandleW.argtypes=[w.HANDLE,w.LPWSTR,w.DWORD,w.DWORD]; "
                "k.GetFinalPathNameByHandleW.restype=w.DWORD; path=ctypes.create_unicode_buffer(4096); "
                "k.GetFinalPathNameByHandleW(int(sys.argv[2]),path,4096,0); "
                "inherited=bool(k.GetHandleInformation(int(sys.argv[2]),ctypes.byref(flags))); "
                "data=sys.stdin.buffer.read(2049); "
                "Path(sys.argv[1]).write_text(json.dumps({'input':data.decode(),"
                "'argv':sys.argv,'unrelated_number_exists':inherited,'unrelated_path':path.value}))"
            )
            job = BrowserTreeJob()
            query = job.query_handle()
            try:
                pid = job.launch([sys._base_executable, "-c", script, str(output),
                                  str(unrelated_handle)], private_input=payload)
                self.assertGreater(pid, 0)
                eventually(output.exists)
                eventually(lambda: query.status() == "EXITED")
                result = json.loads(output.read_text())
                self.assertEqual(result["input"], payload.decode())
                # A numeric handle can be reused by the child's runtime.
                # Only the same kernel file identity indicates inheritance.
                self.assertNotEqual(result["unrelated_path"].removeprefix("\\\\?\\"),
                                    str(unrelated_file.resolve()))
                self.assertNotIn("synthetic-private-profile", " ".join(result["argv"]))
                self.assertEqual(query.accounting()["terminated"], 0)
            finally:
                query.close()
                job.close()
                os.close(unrelated_read)

    def test_invalid_input_is_unlaunched_unknown_and_cannot_retry(self):
        for payload in (b"", b"x" * 2049, "private", bytearray(b"private")):
            with self.subTest(kind=type(payload).__name__, size=len(payload)):
                job = BrowserTreeJob()
                query = job.query_handle()
                try:
                    with self.assertRaisesRegex(ValueError, "^TREE_LAUNCH_UNAVAILABLE$"):
                        job.launch([sys._base_executable, "-c", "pass"], private_input=payload)
                    self.assertEqual(query.accounting(), {"total": 0, "active": 0, "terminated": 0})
                    self.assertEqual(query.status(), "UNKNOWN")
                    with self.assertRaisesRegex(ValueError, "^TREE_LAUNCH_UNAVAILABLE$"):
                        job.launch([sys._base_executable, "-c", "pass"], private_input=b"valid")
                finally:
                    query.close()
                    job.close()

    def test_maximum_input_delivers_without_waiting_for_child_reader(self):
        # This detects prelaunch pipe-buffer deadlock and payload truncation.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "maximum.json"
            script = ("import sys,json; from pathlib import Path; "
                      "data=sys.stdin.buffer.read(2049); "
                      "Path(sys.argv[1]).write_text(json.dumps({'size':len(data),"
                      "'exact':data==b'x'*2048}))")
            job = BrowserTreeJob()
            query = job.query_handle()
            try:
                job.launch([sys._base_executable, "-c", script, str(output)], private_input=b"x" * 2048)
                eventually(output.exists)
                eventually(lambda: query.status() == "EXITED")
                self.assertEqual(json.loads(output.read_text()), {"size": 2048, "exact": True})
                self.assertEqual(query.accounting()["terminated"], 0)
            finally:
                query.close()
                job.close()

    def test_failed_native_launch_closes_only_owned_input_descriptors(self):
        original_pipe = os.pipe
        descriptors = []

        def tracked_pipe():
            result = original_pipe()
            descriptors.extend(result)
            return result

        job = BrowserTreeJob()
        query = job.query_handle()
        try:
            with patch("services.applications.browser_tree.os.pipe", side_effect=tracked_pipe):
                with self.assertRaisesRegex(ValueError, "^TREE_LAUNCH_UNAVAILABLE$"):
                    job.launch([str(Path.cwd() / "missing-private-host.exe")], private_input=b"private")
            self.assertEqual(len(descriptors), 2)
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertEqual(query.status(), "UNKNOWN")
        finally:
            query.close()
            job.close()
