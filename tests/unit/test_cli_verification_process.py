"""Cancellation must stop the owned interpreter's descendants too."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cli.verification_checks import _stop


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cancellation contract")
def test_cancel_reaps_child_and_terminates_its_grandchild(tmp_path):
    pid_file = tmp_path / "owned-grandchild.pid"
    script = ("import subprocess,sys,time,pathlib; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")
    process = subprocess.Popen([sys.executable, "-c", script, str(pid_file)])
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
    process = subprocess.Popen([sys.executable, str(fixture), "browser", str(tmp_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
