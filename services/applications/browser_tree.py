"""Windows browser-host launch containment and retained read-only tree proof.

This primitive does not release operation capacity. A later recovery controller
must bind its observations to the durable operation/attempt/owner fence. Closing
handles never kills processes; loss of the last proof handle is UNKNOWN.
"""
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import subprocess
import sys
from threading import RLock


class _Startup(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                ("title", w.LPWSTR), ("x", w.DWORD), ("y", w.DWORD),
                ("width", w.DWORD), ("height", w.DWORD), ("charsX", w.DWORD),
                ("charsY", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reservedSize", w.WORD), ("reservedData", c.c_void_p),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class _StartupEx(c.Structure):
    _fields_ = [("base", _Startup), ("attributes", c.c_void_p)]


class _ProcessInfo(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class _Accounting(c.Structure):
    _fields_ = [("user", c.c_longlong), ("kernel", c.c_longlong),
                ("periodUser", c.c_longlong), ("periodKernel", c.c_longlong),
                ("faults", w.DWORD), ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]


class _Limits(c.Structure):
    _fields_ = [("processTime", c.c_longlong), ("jobTime", c.c_longlong),
                ("flags", w.DWORD), ("minWS", c.c_size_t), ("maxWS", c.c_size_t),
                ("activeLimit", w.DWORD), ("affinity", c.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


def _kernel():
    if sys.platform != "win32":
        raise ValueError("TREE_CONTAINMENT_UNAVAILABLE")
    kernel = c.WinDLL("kernel32", use_last_error=True)
    definitions = [
        ("CreateJobObjectW", [c.c_void_p, w.LPCWSTR], w.HANDLE),
        ("CloseHandle", [w.HANDLE], w.BOOL),
        ("GetCurrentProcess", [], w.HANDLE),
        ("DuplicateHandle", [w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE),
                             w.DWORD, w.BOOL, w.DWORD], w.BOOL),
        ("QueryInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
        ("InitializeProcThreadAttributeList", [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
        ("UpdateProcThreadAttribute", [c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p,
                                      c.c_size_t, c.c_void_p, c.c_void_p], w.BOOL),
        ("DeleteProcThreadAttributeList", [c.c_void_p], None),
        ("CreateProcessW", [w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL,
                           w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(_StartupEx), c.POINTER(_ProcessInfo)], w.BOOL),
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("IsProcessInJob", [w.HANDLE, w.HANDLE, c.POINTER(w.BOOL)], w.BOOL),
    ]
    for name, arguments, result in definitions:
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _require(result):
    if not result:
        raise OSError("Native containment observation unavailable")
    return result


class BrowserTreeQuery:
    """Own a non-inherited QUERY-only handle; never fall back to a root PID."""
    def __init__(self, kernel, handle):
        self._kernel, self._handle = kernel, handle
        self._lock = RLock()

    def accounting(self):
        with self._lock:
            if self._handle is None:
                return None
            value = _Accounting()
            if not self._kernel.QueryInformationJobObject(
                    self._handle, 1, c.byref(value), c.sizeof(value), None):
                return None
            return {"total": value.total, "active": value.active, "terminated": value.terminated}

    def status(self):
        value = self.accounting()
        if value is None or value["total"] == 0:
            return "UNKNOWN"
        return "LIVE" if value["active"] else "EXITED"

    def contains(self, pid):
        with self._lock:
            if self._handle is None or type(pid) is not int or not 0 < pid <= 0xffffffff:
                return None
            process = self._kernel.OpenProcess(0x1000, False, pid)
            if not process:
                return None
            try:
                value = w.BOOL()
                if not self._kernel.IsProcessInJob(process, self._handle, c.byref(value)):
                    return None
                return bool(value.value)
            finally:
                self._kernel.CloseHandle(process)

    def limit_flags(self):
        with self._lock:
            if self._handle is None:
                return None
            value = _Limits()
            if not self._kernel.QueryInformationJobObject(
                    self._handle, 2, c.byref(value), c.sizeof(value), None):
                return None
            return value.flags

    def close(self):
        with self._lock:
            if self._handle is not None:
                handle, self._handle = self._handle, None
                self._kernel.CloseHandle(handle)


class BrowserTreeJob:
    """Single atomic launch, without assign-after-launch or kill-on-close.

    Launch a dedicated host, never the current API. Descendants inherit the Job
    association. A retained query handle must precede creator handle closure.
    No new launch is allowed after either a successful or ambiguous attempt.
    """
    def __init__(self):
        self._handle = None
        self._attempted = False
        self._lock = RLock()
        try:
            self._kernel = _kernel()
            # Unnamed, non-inherited Job: no reopen-by-name or collision trust.
            self._handle = _require(self._kernel.CreateJobObjectW(None, None))
        except Exception:
            raise ValueError("TREE_CONTAINMENT_UNAVAILABLE") from None

    def query_handle(self):
        with self._lock:
            handle = w.HANDLE()
            try:
                if self._handle is None:
                    raise ValueError
                process = self._kernel.GetCurrentProcess()
                _require(self._kernel.DuplicateHandle(process, self._handle, process,
                                                      c.byref(handle), 4, False, 0))
                return BrowserTreeQuery(self._kernel, handle.value)
            except Exception:
                raise ValueError("TREE_QUERY_UNAVAILABLE") from None

    def launch(self, argv, *, cwd=None):
        with self._lock:
            try:
                if self._handle is None or self._attempted:
                    raise ValueError
                self._attempted = True
                if (not isinstance(argv, (list, tuple)) or not argv
                        or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
                        or not Path(argv[0]).is_absolute()):
                    raise ValueError
                return self._launch(argv, cwd)
            except Exception:
                # Do not expose executable, private profile or native errors.
                raise ValueError("TREE_LAUNCH_UNAVAILABLE") from None

    def _launch(self, argv, cwd):
        kernel = self._kernel
        size = c.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
        if c.get_last_error() != 122 or not size.value:
            raise OSError
        buffer = c.create_string_buffer(size.value)
        _require(kernel.InitializeProcThreadAttributeList(buffer, 1, 0, c.byref(size)))
        try:
            jobs = (w.HANDLE * 1)(self._handle)
            # PROC_THREAD_ATTRIBUTE_JOB_LIST: admission in CreateProcess itself.
            _require(kernel.UpdateProcThreadAttribute(buffer, 0, 0x2000D, jobs,
                                                      c.sizeof(jobs), None, None))
            startup = _StartupEx()
            startup.base.cb = c.sizeof(startup)
            startup.attributes = c.cast(buffer, c.c_void_p)
            info = _ProcessInfo()
            command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW; no handle inheritance.
            _require(kernel.CreateProcessW(argv[0], command, None, None, False,
                                            0x80000 | 0x08000000, None,
                                            str(cwd) if cwd is not None else None,
                                            c.byref(startup), c.byref(info)))
            try:
                return info.pid
            finally:
                kernel.CloseHandle(info.thread)
                kernel.CloseHandle(info.process)
        finally:
            kernel.DeleteProcThreadAttributeList(buffer)

    def close(self):
        with self._lock:
            if self._handle is not None:
                handle, self._handle = self._handle, None
                self._kernel.CloseHandle(handle)
