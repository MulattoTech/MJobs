"""Read-only Chromium root identity; uncertain OS observations never prove exit.

This is a recovery prerequisite, not authorization to release capacity or kill a
process. Only a captured CDP root with the exact requested profile is accepted.
"""
from hashlib import sha256
from pathlib import Path
import os
import sys


def _host_identity():
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            machine = winreg.QueryValueEx(key, "MachineGuid")[0]
        scope = "win32:" + machine
    elif sys.platform == "linux":
        # Include the PID namespace and boot so another container/reboot cannot
        # mistake the absence of a local PID for the original process's exit.
        scope = ":".join((Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                          os.readlink("/proc/self/ns/pid")))
    else:
        raise OSError("Unsupported process identity runtime")
    if not scope.strip():
        raise OSError("Missing host identity")
    return sha256(scope.encode()).hexdigest()


def _read_process(pid):
    """Return immutable creation identity and state, or None for an absent PID."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
                return None
            raise OSError(error, "Process identity unavailable")
        try:
            created, exited, system, user = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                raise OSError("Process creation time unavailable")
            wait = kernel.WaitForSingleObject(handle, 0)
            if wait not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT
                raise OSError("Process exit observation unavailable")
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime), (
                "EXITED" if wait == 0 else "LIVE")
        finally:
            kernel.CloseHandle(handle)
    if sys.platform == "linux":
        try:
            value = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return None
        # comm may contain spaces and parentheses; fields after its last ')' are
        # state (3) onward. starttime is field22 and is immutable for this PID.
        fields = value[value.rindex(")") + 2:].split()
        return fields[19], "EXITED" if fields[0] in ("Z", "X") else "LIVE"
    raise OSError("Unsupported process identity runtime")


def valid_identity(identity):
    return (isinstance(identity, dict) and set(identity) == {"pid", "created", "host", "profile_sha256"}
            and type(identity["pid"]) is int and 0 < identity["pid"] <= 0xffffffff
            and isinstance(identity["created"], str) and identity["created"].isascii()
            and identity["created"].isdigit() and 0 < len(identity["created"]) <= 32
            and isinstance(identity["host"], str) and len(identity["host"]) == 64
            and all(character in "0123456789abcdef" for character in identity["host"])
            and isinstance(identity["profile_sha256"], str) and len(identity["profile_sha256"]) == 64
            and all(character in "0123456789abcdef" for character in identity["profile_sha256"]))


def process_status(identity):
    """LIVE, EXITED or UNKNOWN; PID reuse and unreadable/foreign owners fail closed."""
    if not valid_identity(identity):
        return "UNKNOWN"
    try:
        if identity["host"] != _host_identity():
            return "UNKNOWN"
        current = _read_process(identity["pid"])
        if current is None:
            return "EXITED"
        created, state = current
        return state if created == identity["created"] else "UNKNOWN"
    except (OSError, ValueError, IndexError):
        return "UNKNOWN"


def capture_browser_identity(context, profile):
    """Capture the public CDP root PID only after exact profile verification."""
    session = None
    try:
        session = context.browser.new_browser_cdp_session()
        arguments = session.send("Browser.getBrowserCommandLine")["arguments"]
        profiles = [argument.partition("=")[2] for argument in arguments
                    if isinstance(argument, str) and argument.startswith("--user-data-dir=")]
        if len(profiles) != 1 or Path(profiles[0]).resolve() != Path(profile).resolve():
            raise ValueError
        roots = [item["id"] for item in session.send("SystemInfo.getProcessInfo")["processInfo"]
                 if item.get("type") == "browser"]
        if len(roots) != 1 or type(roots[0]) is not int or not 0 < roots[0] <= 0xffffffff:
            raise ValueError
        current = _read_process(roots[0])
        if current is None or current[1] != "LIVE":
            raise ValueError
        identity = {"pid": roots[0], "created": current[0], "host": _host_identity(),
                    "profile_sha256": sha256(os.path.normcase(str(Path(profile).resolve())).encode()).hexdigest()}
        if not valid_identity(identity):
            raise ValueError
        return identity
    except Exception:
        # CDP errors/arguments can contain the private profile path. Never
        # propagate those values through public errors or operational logs.
        raise ValueError("BROWSER_IDENTITY_UNAVAILABLE") from None
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
