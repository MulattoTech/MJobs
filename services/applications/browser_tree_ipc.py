"""Private Windows observer bootstrap and bound read-only observations.

Anonymous pipes authenticate the already-created child by possession; no public
endpoint, pickle, command-line secret, inherited API environment or PID lookup
is used to transfer authority. This is not durable closure evidence or capacity
release. Channel loss is UNKNOWN, including after creator death.
"""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
from secrets import token_hex
import subprocess
import sys
from threading import RLock
import time


_MAX_FRAME = 8192
_MAX_SEND_FRAME = 2048
_ERROR = "TREE_OBSERVER_UNAVAILABLE"


class _ChannelLost(ValueError):
    pass


def validate_binding(value):
    fields = {"operation_id", "application_id", "attempt_id", "profile_id", "owner_digest"}
    if (not isinstance(value, dict) or set(value) != fields
            or any(not isinstance(v, str) or not v or not v.isascii() or len(v) > 256
                   or any(not (character.isalnum() or character in "-_.:") for character in v)
                   for v in value.values())
            or len(value["owner_digest"]) != 64
            or any(character not in "0123456789abcdef" for character in value["owner_digest"])):
        raise ValueError(_ERROR)
    return dict(value)


def _status(accounting):
    if accounting is None:
        return "UNKNOWN"
    if (not isinstance(accounting, dict) or set(accounting) != {"total", "active", "terminated"}
            or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in accounting.values())
            or accounting["active"] > accounting["total"]
            or accounting["terminated"] > accounting["total"]):
        raise ValueError(_ERROR)
    if accounting["total"] == 0:
        return "UNKNOWN"
    return "LIVE" if accounting["active"] else "EXITED"


def validate_reply(value, nonce, binding, identity):
    if (not isinstance(value, dict)
            or set(value) != {"nonce", "binding", "identity", "status", "accounting"}
            or value["nonce"] != nonce or value["binding"] != binding
            or value["identity"] != identity or value["status"] != _status(value["accounting"])):
        raise ValueError(_ERROR)
    return value


def _write(stream, value):
    frame = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    # Scope IDs are ASCII and bounded; one outstanding request plus the close
    # message must fit the private subprocess pipe even if the peer stalls.
    # Keep outbound frames smaller than the receive limit; do not expand this
    # without implementing cancellable writes.
    if len(frame) > _MAX_SEND_FRAME:
        raise ValueError(_ERROR)
    # A raw pipe write can be partial; flush only after the entire frame.
    offset = 0
    while offset < len(frame):
        written = stream.write(frame[offset:])
        if not written:
            raise _ChannelLost(_ERROR)
        offset += written
    stream.flush()


def _read(stream, seconds):
    """Bound native reads even when a peer sends only part of a JSON frame."""
    import msvcrt

    kernel = c.WinDLL("kernel32", use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.c_void_p, c.POINTER(w.DWORD), c.c_void_p]
    peek.restype = w.BOOL
    handle = msvcrt.get_osfhandle(stream.fileno())
    deadline = time.monotonic() + seconds
    frame = bytearray()
    while time.monotonic() < deadline:
        available = w.DWORD()
        if not peek(handle, None, 0, None, c.byref(available), None):
            raise _ChannelLost(_ERROR)
        if available.value:
            character = os.read(stream.fileno(), 1)
            if character == b"\n":
                return json.loads(frame)
            if not character or len(frame) >= _MAX_FRAME - 1:
                raise ValueError(_ERROR)
            frame.extend(character)
        else:
            time.sleep(.01)
    raise ValueError(_ERROR)


class BrowserTreeObserver:
    """One trusted outside-Job observer, scoped before launch admission.

    Must be bootstrapped before relying on creator-independent proof. The
    operation's stored owner fence remains the caller's responsibility. Never
    use this transient channel reply as a durable recovery certificate.
    """
    def __init__(self, query, binding):
        self._lock = RLock()
        self._process = None
        self._closed = False
        self._binding = validate_binding(binding)
        self.identity = None
        try:
            if sys.platform != "win32":
                raise ValueError
            from services.applications.browser_process import _read_process, _host_identity
            from services.applications.browser_tree import _host_environment

            # The Windows venv redirector PID is not the native interpreter.
            # Launch only our installed script, with private process-specific
            # anonymous pipes; no listener or discovery-by-PID transfer.
            self._process = subprocess.Popen(
                [sys._base_executable, str(Path(__file__).resolve())],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, close_fds=True, env=_host_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW)
            current = _read_process(self._process.pid)
            if current is None or current[1] != "LIVE":
                raise ValueError
            self.identity = {"pid": self._process.pid, "created": current[0], "host": _host_identity()}
            nonce = token_hex(32)
            handle = query.duplicate_to(int(self._process._handle))
            _write(self._process.stdin, {"handle": handle, "binding": self._binding, "nonce": nonce})
            reply = _read(self._process.stdout, 5)
            validate_reply(reply, nonce, self._binding, self.identity)
            # Native membership is checked by the recipient before ACK. A
            # failed ACK never permits a browser launch/release by this class.
        except Exception:
            self.close()
            raise ValueError(_ERROR) from None

    def observe(self, binding=None):
        with self._lock:
            if binding is not None and validate_binding(binding) != self._binding:
                raise ValueError(_ERROR)
            unknown = {"status": "UNKNOWN", "accounting": None, "binding": dict(self._binding)}
            if self._closed:
                return unknown
            try:
                nonce = token_hex(32)
                _write(self._process.stdin, {"action": "observe", "nonce": nonce, "binding": self._binding})
                reply = _read(self._process.stdout, 2)
                validate_reply(reply, nonce, self._binding, self.identity)
                return {key: reply[key] for key in ("status", "accounting", "binding")}
            except Exception:
                self.close()
                return unknown

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process is None:
                return
            try:
                if process.poll() is None:
                    _write(process.stdin, {"action": "close"})
            except (OSError, ValueError):
                pass
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # No termination: closing query authority must never kill a
                # browser, and a lost/blocked observer stays UNKNOWN.
                pass


def _serve():
    # Bootstrap import is an absolute repository-owned script, not PYTHONPATH
    # received from the API environment or any wire-provided module name.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from services.applications.browser_process import capture_owner_identity
    from services.applications.browser_tree import BrowserTreeQuery, _kernel

    query = None
    try:
        initial = _read(sys.stdin.buffer, 5)
        if (not isinstance(initial, dict) or set(initial) != {"handle", "nonce", "binding"}
                or type(initial["handle"]) is not int or not 0 < initial["handle"] < 2 ** (c.sizeof(w.HANDLE) * 8)
                or not isinstance(initial["nonce"], str) or len(initial["nonce"]) != 64
                or any(v not in "0123456789abcdef" for v in initial["nonce"])):
            raise ValueError
        binding = validate_binding(initial["binding"])
        query = BrowserTreeQuery(_kernel(), initial["handle"])
        if query.contains(os.getpid()) is not False or query.limit_flags() != 0:
            raise ValueError
        identity = capture_owner_identity()

        def respond(nonce):
            accounting = query.accounting()
            _write(sys.stdout.buffer, {"nonce": nonce, "binding": binding, "identity": identity,
                                      "status": _status(accounting), "accounting": accounting})

        respond(initial["nonce"])
        while True:
            # Finite lifetime, including an abandoned creator. No certification
            # is inferred from reaching the deadline or losing the pipe.
            try:
                request = _read(sys.stdin.buffer, 900)
            except _ChannelLost:
                # Retain authority outside the departed creator until bounded
                # cooperative tree exit. No transient reply/certificate can be
                # recovered over a lost channel; capacity must stay held.
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    if query.status() == "EXITED":
                        break
                    time.sleep(.05)
                return
            if request == {"action": "close"}:
                return
            if (not isinstance(request, dict) or set(request) != {"action", "nonce", "binding"}
                    or request["action"] != "observe" or request["binding"] != binding
                    or not isinstance(request["nonce"], str) or len(request["nonce"]) != 64
                    or any(v not in "0123456789abcdef" for v in request["nonce"])):
                raise ValueError
            respond(request["nonce"])
    except Exception:
        # Value-free exit; neither paths nor bootstrap challenges are logged.
        return
    finally:
        if query is not None:
            query.close()


if __name__ == "__main__":
    _serve()
