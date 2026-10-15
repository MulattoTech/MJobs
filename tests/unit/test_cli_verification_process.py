"""Cancellation must stop the owned interpreter's descendants too."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cli.verification_checks import _stop
from cli.verification_process import OwnedProcess


@pytest.mark.skipif(os.name != "nt", reason="Windows retained process identity regression")
@pytest.mark.parametrize("valid_result", [False, True])
def test_exited_root_cannot_leave_descendant_or_touch_unrelated_process(tmp_path, monkeypatch, valid_result):
    from cli.verification_checks import run_connected, exit_status

    pid_file, release = tmp_path / "descendant.pid", tmp_path / "release-root"
    script = '''
import json,os,pathlib,subprocess,sys,time
from datetime import datetime,timezone
request,output,pidfile,release,valid=map(pathlib.Path,sys.argv[1:])
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
pidfile.write_text(str(child.pid))
while not release.exists(): time.sleep(.01)
if str(valid)=='yes':
 r=json.loads(request.read_text())
 r['runtime']={'pid':os.getpid(),'parent_pid':os.getppid(),'python':sys.version.split()[0]}
 r['finished_at']=datetime.now(timezone.utc).isoformat()
 output.write_text(json.dumps(r))
sys.exit(2 if str(valid)=='yes' else 1)
'''
    unrelated = OwnedProcess([sys.executable, "-c", "import time; time.sleep(60)"], env=dict(os.environ))

    def launch(argv, **kwargs):
        return OwnedProcess([sys.executable, "-c", script, argv[3], argv[4],
                           str(pid_file), str(release), "yes" if valid_result else "no"], **kwargs)

    monkeypatch.setattr("cli.verification_checks.OwnedProcess", launch)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(run_connected, fixture=True, timeout=15)
            deadline = time.monotonic() + 10
            while not pid_file.exists():
                assert time.monotonic() < deadline
                time.sleep(.02)
            # Root is held alive while this original descendant identity is
            # opened and retained; cleanup never targets its numeric PID.
            handle = kernel.OpenProcess(0x100001, False, int(pid_file.read_text()))
            assert handle and kernel.WaitForSingleObject(handle, 0) == 258
            release.touch()
            report = result.result(timeout=20)
        assert kernel.WaitForSingleObject(handle, 5000) == 0, "exited verifier root leaked its owned descendant"
        assert unrelated.poll() is None, "cleanup must not affect unrelated processes"
        assert report["cleanup"]["status"] == "PASS"
        assert exit_status(report) == (2 if valid_result else 1)
        assert not Path(report["workspace"]).exists()
    finally:
        release.touch()
        if handle:
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        unrelated.finalize()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cancellation contract")
def test_cancel_reaps_child_and_terminates_its_grandchild(tmp_path):
    pid_file = tmp_path / "owned-grandchild.pid"
    script = ("import subprocess,sys,time,pathlib; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    process = OwnedProcess([sys.executable, "-c", script, str(pid_file)], env=dict(os.environ))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = None
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        handle = kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
        assert handle, "test must own a live descendant handle before cancellation"
        assert kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT: still alive
        _stop(process)
        assert process.poll() is not None
        assert kernel.WaitForSingleObject(handle, 5000) == 0  # signaled: exited
    finally:
        if process.poll() is None:
            _stop(process)
        if handle:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows owned Chromium tree cancellation")
def test_cancel_terminates_actual_owned_chromium_members(tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("matching cached Chromium unavailable")
    # Existing approved synthetic browser harness: blocks network, creates its
    # own temporary profile and records only this browser's CDP process IDs.
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "browser_tree_child.py"
    process = OwnedProcess([sys.executable, str(fixture), "browser", str(tmp_path)], env=dict(os.environ))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handles = []
    try:
        ready = tmp_path / "ready.json"
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert process.poll() is None, "owned browser harness exited before ready"
            assert time.monotonic() < deadline
            time.sleep(0.05)
        members = json.loads(ready.read_text())["members"]
        assert members
        for pid in members:
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                handles.append(handle)
        assert handles, "must hold actual browser member handles before cancellation"
        _stop(process)
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
    finally:
        if process.poll() is None:
            _stop(process)
        for handle in handles:
            kernel.CloseHandle(handle)
