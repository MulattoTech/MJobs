"""Disposable outside-Job observer and exiting creator for native proof tests."""
import json
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.applications.browser_process import capture_owner_identity
from services.applications.browser_tree import BrowserTreeJob, BrowserTreeQuery, _Limits, _kernel


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def main():
    role, location, scenario = sys.argv[1:]
    directory = Path(location)
    if role == "observer":
        handle = int(sys.stdin.readline())
        query = BrowserTreeQuery(_kernel(), handle)
        try:
            setter = query._kernel.SetInformationJobObject
            setter.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
            setter.restype = w.BOOL
            limits = _Limits()
            c.set_last_error(0)
            # Reapplying the existing zero flags is harmless if unexpectedly
            # accepted, but must be denied on the transferred QUERY handle.
            set_allowed = bool(setter(handle, 2, c.byref(limits), c.sizeof(limits)))
            set_error = c.get_last_error()
            write(directory / "observer-ready.json", {
                "identity": capture_owner_identity(),
                "contained": query.contains(os.getpid()), "limits": query.limit_flags(),
                "setAllowed": set_allowed, "setError": set_error})
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                write(directory / "observation.json", {
                    "status": query.status(), "accounting": query.accounting()})
                if query.status() == "EXITED" or (directory / "observer-release").exists():
                    return
                time.sleep(.05)
        finally:
            query.close()
    else:
        job = BrowserTreeJob()
        query = job.query_handle()
        # Use the native executable, not the Windows venv redirector: the
        # Popen process handle must identify the observer interpreter itself.
        observer = subprocess.Popen(
            [sys._base_executable, __file__, "observer", location, scenario],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        handle = query.duplicate_to(int(observer._handle))
        observer.stdin.write(str(handle) + "\n")
        observer.stdin.close()
        deadline = time.monotonic() + 10
        while not (directory / "observer-ready.json").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Observer readiness unavailable")
            time.sleep(.05)
        job.launch([sys.executable, str(Path(__file__).with_name("browser_tree_child.py")),
                    scenario, location], cwd=Path.cwd())
        write(directory / "creator.json", capture_owner_identity())
        # Simulate an owner crash without killing any unrelated process.
        # All creator handles disappear; observer and descendants retain life.
        os._exit(0)


if __name__ == "__main__":
    main()
