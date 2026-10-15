"""Windows-only owned verifier Job, admitted atomically during CreateProcess.

Never breaks out of an enclosing Job or targets processes by PID. Native ABI
structures are reused read-only from the existing browser containment harness.
Other platforms fail closed until an equivalent retained-identity owner exists.
"""
import ctypes as c
from ctypes import wintypes as w
import os
import subprocess
import sys
import time

from services.applications.browser_tree import _Accounting, _Limits, _ProcessInfo, _StartupEx, _kernel


class ContainmentUnavailable(RuntimeError):
    pass


class CleanupError(RuntimeError):
    pass


class _ExtendedLimits(c.Structure):
    _fields_ = [("basic", _Limits), ("io", c.c_ulonglong * 6),
                ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]


class OwnedProcess:
    """Retain original host and non-inherited Job handles across leader exit."""
    def __init__(self, argv, *, cwd=None, env=None):
        self._job = self._host = None
        self.pid = None
        self.returncode = None
        if sys.platform != "win32":
            raise ContainmentUnavailable("retained verifier tree ownership unavailable on this platform")
        self._kernel = _kernel()
        for name, args, result in (
            ("SetInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
        ):
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = args, result
        try:
            self._job = self._kernel.CreateJobObjectW(None, None)
            self._require(self._job)
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway flags
            self._require(self._kernel.SetInformationJobObject(self._job, 9, c.byref(limits), c.sizeof(limits)))
            self._launch(argv, cwd, env or {})
        except BaseException:
            if self._host is not None:
                self.finalize()
            elif self._job is not None:
                self._kernel.CloseHandle(self._job)
                self._job = None
            raise ContainmentUnavailable("atomic nested verifier Job launch unavailable") from None

    @staticmethod
    def _require(value):
        if not value:
            raise OSError("owned verifier native operation unavailable")
        return value

    def _launch(self, argv, cwd, env):
        import msvcrt

        kernel = self._kernel
        size = c.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
        if c.get_last_error() != 122 or not size.value:
            raise OSError("attribute allocation unavailable")
        buffer = c.create_string_buffer(size.value)
        self._require(kernel.InitializeProcThreadAttributeList(buffer, 2, 0, c.byref(size)))
        null_fd = None
        try:
            jobs = (w.HANDLE * 1)(self._job)
            self._require(kernel.UpdateProcThreadAttribute(buffer, 0, 0x2000D, jobs, c.sizeof(jobs), None, None))
            null_fd = os.open(os.devnull, os.O_RDWR)
            null_handle = msvcrt.get_osfhandle(null_fd)
            os.set_handle_inheritable(null_handle, True)
            handles = (w.HANDLE * 1)(null_handle)
            self._require(kernel.UpdateProcThreadAttribute(buffer, 0, 0x20002, handles, c.sizeof(handles), None, None))
            startup = _StartupEx()
            startup.base.cb = c.sizeof(startup)
            startup.attributes = c.cast(buffer, c.c_void_p)
            startup.base.flags = 0x100
            startup.base.stdin = startup.base.stdout = startup.base.stderr = null_handle
            info = _ProcessInfo()
            command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            block = c.create_unicode_buffer("\0".join(f"{key}={env[key]}" for key in sorted(env)) + "\0\0")
            # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT.
            # The nested Job attribute is applied before ANY child code can run.
            self._require(kernel.CreateProcessW(argv[0], command, None, None, True,
                                               0x80000 | 0x08000000 | 0x400, block,
                                               str(cwd) if cwd else None, c.byref(startup), c.byref(info)))
            self._host, self.pid = info.process, info.pid
            kernel.CloseHandle(info.thread)
        finally:
            if null_fd is not None:
                os.close(null_fd)
            kernel.DeleteProcThreadAttributeList(buffer)

    def poll(self):
        if self._host is None:
            return self.returncode
        state = self._kernel.WaitForSingleObject(self._host, 0)
        if state == 258:
            return None
        self._require(state == 0)
        code = w.DWORD()
        self._require(self._kernel.GetExitCodeProcess(self._host, c.byref(code)))
        self.returncode = code.value
        return self.returncode

    def wait(self, timeout):
        state = self._kernel.WaitForSingleObject(self._host, max(0, int(timeout * 1000)))
        if state == 258:
            raise subprocess.TimeoutExpired("owned verifier", timeout)
        self._require(state == 0)
        return self.poll()

    def finalize(self):
        """Terminate this retained Job even if the original host already exited.

        PASS is available only after Job accounting reports zero active members
        and the original host handle is signaled. Close never grants success
        when native observation or termination fails.
        """
        if self._job is None:
            raise CleanupError("owned Job proof already unavailable")
        try:
            self._require(self._kernel.TerminateJobObject(self._job, 1))
            deadline = time.monotonic() + 10
            while True:
                accounting = _Accounting()
                self._require(self._kernel.QueryInformationJobObject(
                    self._job, 1, c.byref(accounting), c.sizeof(accounting), None))
                if accounting.active == 0:
                    break
                if time.monotonic() >= deadline:
                    raise CleanupError("owned Job termination could not be proven")
                time.sleep(.02)
            self.wait(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            raise CleanupError("owned verifier tree cleanup could not be proven") from None
        finally:
            # Closing the last owned Job handle also enforces kill-on-close,
            # but is not substituted for the explicit accounting proof above.
            if self._host is not None:
                self._kernel.CloseHandle(self._host)
                self._host = None
            self._kernel.CloseHandle(self._job)
            self._job = None
