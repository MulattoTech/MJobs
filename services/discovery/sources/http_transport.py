"""Request-owned cancellable sockets for public source HTTP requests.

HTTPX/httpcore are pinned. Install a backend on this transport only, retaining
their HTTP protocol, TLS context/verification, redirect and timeout semantics.
No resolver threads survive cancellation: each DNS lookup has an owned child.
"""
import errno
import ipaddress
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import threading
import time

import httpcore
import httpx


_POLL = .02
_DNS_SLOTS = threading.BoundedSemaphore(4)
_MAX_ADDRESSES = 8
_RESOLVER_CODE = (
    "import json,socket,sys; "
    "print(json.dumps(socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),"
    "type=socket.SOCK_STREAM)[:8]))"
)
_PENDING = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR,
            getattr(errno, "WSAEINTR", 10004),
            getattr(errno, "WSAEWOULDBLOCK", 10035),
            getattr(errno, "WSAEINPROGRESS", 10036), getattr(errno, "WSAEALREADY", 10037)}


def _remaining(check, deadline, timeout_error):
    check()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise timeout_error("Source socket phase timed out")
    return remaining


def _deadline(timeout):
    return float("inf") if timeout is None else time.monotonic() + max(0, timeout)


def _resolve(host, port, check, deadline):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        target = (str(address), port, 0, 0) if address.version == 6 else (str(address), port)
        return [(family, socket.SOCK_STREAM, 0, "", target)]

    while not _DNS_SLOTS.acquire(timeout=_POLL):
        _remaining(check, deadline, httpcore.ConnectTimeout)
    process = None
    try:
        _remaining(check, deadline, httpcore.ConnectTimeout)
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _RESOLVER_CODE, host, str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
                 if key in os.environ},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while process.poll() is None:
            remaining = _remaining(check, deadline, httpcore.ConnectTimeout)
            time.sleep(min(_POLL, remaining))
        _remaining(check, deadline, httpcore.ConnectTimeout)
        output, _ = process.communicate()
        if process.returncode:
            raise httpcore.ConnectError("Source DNS resolution failed")
        return [(family, kind, proto, name, tuple(target))
                for family, kind, proto, name, target in json.loads(output)[:_MAX_ADDRESSES]]
    except (OSError, ValueError) as error:
        _remaining(check, deadline, httpcore.ConnectTimeout)
        raise httpcore.ConnectError("Source DNS resolution unavailable") from error
    finally:
        try:
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=.2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                finally:
                    if process.stdout is not None:
                        process.stdout.close()
        finally:
            _DNS_SLOTS.release()


class _Stream(httpcore.NetworkStream):
    def __init__(self, connection, check):
        self.connection, self.check = connection, check

    def _wait(self, reading, deadline, timeout_error):
        remaining = _remaining(self.check, deadline, timeout_error)
        select.select([self.connection] if reading else [],
                      [] if reading else [self.connection], [], min(_POLL, remaining))

    def _perform(self, action, *, reading, deadline, timeout_error, io_error):
        while True:
            _remaining(self.check, deadline, timeout_error)
            try:
                try:
                    return action()
                except ssl.SSLWantReadError:
                    self._wait(True, deadline, timeout_error)
                except ssl.SSLWantWriteError:
                    self._wait(False, deadline, timeout_error)
                except BlockingIOError:
                    self._wait(reading, deadline, timeout_error)
            except (OSError, ValueError) as error:
                self.check()  # Preserve cancellation instead of classifying a close race as network failure.
                raise io_error("Source socket I/O failed") from error

    def read(self, max_bytes, timeout=None):
        return self._perform(lambda: self.connection.recv(max_bytes), reading=True,
                             deadline=_deadline(timeout), timeout_error=httpcore.ReadTimeout,
                             io_error=httpcore.ReadError)

    def write(self, buffer, timeout=None):
        deadline = _deadline(timeout)
        pending = memoryview(buffer)
        while pending:
            sent = self._perform(lambda: self.connection.send(pending), reading=False,
                                 deadline=deadline, timeout_error=httpcore.WriteTimeout,
                                 io_error=httpcore.WriteError)
            if sent == 0:
                raise httpcore.WriteError("Source socket closed during write")
            pending = pending[sent:]

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        deadline = _deadline(timeout)
        try:
            self.check()
            self.connection = ssl_context.wrap_socket(
                self.connection, server_hostname=server_hostname, do_handshake_on_connect=False)
            self.connection.setblocking(False)
            self._perform(self.connection.do_handshake, reading=True, deadline=deadline,
                          timeout_error=httpcore.ConnectTimeout, io_error=httpcore.ConnectError)
            return self
        except BaseException:
            self.close()
            raise

    def get_extra_info(self, info):
        if info == "socket":
            return self.connection
        if info == "ssl_object":
            return self.connection._sslobj if isinstance(self.connection, ssl.SSLSocket) else None
        if info == "is_readable":
            return bool(select.select([self.connection], [], [], 0)[0])
        if info == "client_addr":
            return self.connection.getsockname()
        if info == "server_addr":
            return self.connection.getpeername()
        return None

    def close(self):
        self.connection.close()


class _Backend(httpcore.NetworkBackend):
    def __init__(self, check):
        self.check = check

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        deadline = _deadline(timeout)
        addresses = _resolve(host, port, self.check, deadline)
        options = tuple(socket_options or ())
        last_error = None
        pending = []
        try:
            # All bounded candidates share the source's configured connect
            # deadline. An unreachable first family cannot starve a healthy
            # alternative. Only the winning connection receives HTTP bytes.
            for family, kind, proto, _name, target in addresses[:_MAX_ADDRESSES]:
                _remaining(self.check, deadline, httpcore.ConnectTimeout)
                connection = None
                try:
                    connection = socket.socket(family, kind, proto)
                    pending.append(connection)  # Own it before any fallible setup.
                    connection.setblocking(False)
                    for option in options:
                        connection.setsockopt(*option)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    if local_address is not None:
                        connection.bind((local_address, 0))
                    status = connection.connect_ex(target)
                    if status in (0, errno.EISCONN):
                        pending.remove(connection)
                        return _Stream(connection, self.check)
                    if status not in _PENDING:
                        raise OSError(status, "Source connection failed")
                except (OSError, ValueError) as error:
                    last_error = error
                    if connection is not None:
                        pending.remove(connection)
                        connection.close()
                    self.check()
            while pending:
                remaining = _remaining(self.check, deadline, httpcore.ConnectTimeout)
                _, writable, exceptional = select.select([], pending, pending, min(_POLL, remaining))
                for connection in set(writable + exceptional):
                    status = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    pending.remove(connection)
                    if not status:
                        return _Stream(connection, self.check)
                    last_error = OSError(status, "Source connection failed")
                    connection.close()
            raise httpcore.ConnectError("All bounded source connection attempts failed") from last_error
        finally:
            for connection in pending:
                connection.close()


class SourceTransport(httpx.HTTPTransport):
    def __init__(self, check, *, verify=True):
        super().__init__(verify=verify, trust_env=False)
        self._pool._network_backend = _Backend(check)
