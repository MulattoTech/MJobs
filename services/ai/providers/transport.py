"""Request-owned transport cleanup shared by HTTP AI providers."""
import errno
import httpcore
from httpcore._backends.sync import SyncStream
import select
import socket
from contextlib import contextmanager
import threading
import time

from services.ai.providers import AIProviderError


_MAX_DNS_WORKERS = 4
_MAX_ADDRESS_ATTEMPTS = 8
_DNS_SLOTS = threading.BoundedSemaphore(_MAX_DNS_WORKERS)
_CONNECT_PENDING = {
    errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR,
    getattr(errno, "WSAEINTR", 10004),
    getattr(errno, "WSAEWOULDBLOCK", 10035),
    getattr(errno, "WSAEINPROGRESS", 10036),
    getattr(errno, "WSAEALREADY", 10037),
}


def _bounded_getaddrinfo(host, port, guard):
    """Wait cancellably for DNS while capping uninterruptible OS resolver calls.

    Python cannot cancel a thread already inside getaddrinfo. A daemon worker
    therefore owns that call, and a process-wide semaphore limits the number
    of such workers even if the resolver stalls indefinitely.
    """
    while not _DNS_SLOTS.acquire(blocking=False):
        guard.check()
        remaining = guard.deadline - time.monotonic()
        if guard.cancellation_event is None:
            time.sleep(min(.01, max(0, remaining)))
        else:
            guard.cancellation_event.wait(min(.01, max(0, remaining)))
    result = []
    failure = []
    finished = threading.Event()

    def resolve():
        try:
            result.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception as error:
            failure.append(error)
        finally:
            _DNS_SLOTS.release()
            finished.set()

    try:
        resolver = threading.Thread(target=resolve, name="mjobs-dns-resolver", daemon=True)
        resolver.start()
    except BaseException:
        _DNS_SLOTS.release()
        raise

    while not finished.wait(.01):
        guard.check()
    guard.check()
    if failure:
        raise failure[0]
    return result


class _RequestScopedBackend(httpcore.SyncBackend):
    """HTTPCore sync backend that applies one request guard to DNS and TCP."""

    def __init__(self, guard):
        self.guard = guard

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.guard.check()
        addresses = _bounded_getaddrinfo(host, port, self.guard)
        if not addresses:
            raise httpcore.ConnectError("DNS returned no connection addresses")

        options = tuple(socket_options or ())
        failures = 0
        last_error = None
        for family, socktype, proto, _canonname, address in addresses[:_MAX_ADDRESS_ATTEMPTS]:
            self.guard.check()
            connection = socket.socket(family, socktype, proto)
            try:
                connection.setblocking(False)
                if local_address is not None:
                    connection.bind((local_address, 0))
                self.guard.register_connecting(connection)
                self.guard.check()
                status = connection.connect_ex(address)
                if status not in _CONNECT_PENDING and status not in (0, errno.EISCONN):
                    raise OSError(status, "connection attempt failed")
                if status not in (0, errno.EISCONN):
                    while True:
                        self.guard.check()
                        remaining = self.guard.deadline - time.monotonic()
                        _readable, writable, exceptional = select.select(
                            [], [connection], [connection], min(.01, max(0, remaining)),
                        )
                        if writable or exceptional:
                            error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                            if error:
                                raise OSError(error, "connection attempt failed")
                            break
                for option in options:
                    connection.setsockopt(*option)
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # Keep the socket registered until HTTPCore's immediately
                # following connect-complete trace transfers stream ownership.
                return SyncStream(connection)
            except AIProviderError:
                self.guard.release_connecting(connection, close=True)
                raise
            except (OSError, ValueError, select.error) as error:
                failures += 1
                last_error = error
                self.guard.release_connecting(connection, close=True)
        self.guard.check()
        if failures == 0:
            raise httpcore.ConnectError("No usable connection addresses")
        raise httpcore.ConnectError("All bounded connection attempts failed") from last_error

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while True:
            self.guard.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self.guard.cancellation_event is None:
                time.sleep(min(.01, remaining))
            else:
                self.guard.cancellation_event.wait(min(.01, remaining))


def _shutdown_response_socket(response) -> None:
    """Wake an in-flight read before close; close alone can leave Linux recv blocked.

    HTTPX exposes the request-owned network stream as a documented response
    extension. Custom transports may omit it; their normal close still applies.
    No socket is reused: this request's client is closed immediately afterward.
    """
    if response is None:
        return
    _shutdown_stream_socket(response.extensions.get("network_stream"))


def _shutdown_stream_socket(stream):
    try:
        connection = stream.get_extra_info("socket") if stream is not None else None
        if isinstance(connection, socket.socket):
            connection.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError, TypeError, ValueError, NotImplementedError):
        # A concurrent close or a transport without socket metadata is harmless.
        return


class _RequestGuard:
    def __init__(self, client, deadline, cancellation_event):
        self.client = client
        self.deadline = deadline
        self.cancellation_event = cancellation_event
        self.stream = None
        self.response = None
        self.interrupt_socket = None
        self.connecting_socket = None
        self.interrupted = False
        self.interruption_code = None
        self.lock = threading.Lock()
        self.finished = threading.Event()

    def check(self):
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            self.interrupt("AI_PROVIDER_CANCELLED")
            raise AIProviderError("AI_PROVIDER_CANCELLED")
        if time.monotonic() >= self.deadline:
            self.interrupt("AI_PROVIDER_TIMEOUT")
            raise AIProviderError("AI_PROVIDER_TIMEOUT")

    def register_connecting(self, connection):
        with self.lock:
            if self.cancellation_event is not None and self.cancellation_event.is_set():
                self.interrupted = True
                self.interruption_code = self.interruption_code or "AI_PROVIDER_CANCELLED"
            elif time.monotonic() >= self.deadline:
                self.interrupted = True
                self.interruption_code = self.interruption_code or "AI_PROVIDER_TIMEOUT"
            if self.interrupted:
                code = self.interruption_code or "AI_PROVIDER_UNAVAILABLE"
            else:
                self.connecting_socket = connection
                return
        connection.close()
        raise AIProviderError(code)

    def release_connecting(self, connection, *, close=False):
        with self.lock:
            if self.connecting_socket is connection:
                self.connecting_socket = None
        if close:
            try:
                connection.close()
            except OSError:
                pass

    def trace(self, event, info):
        # HTTPX's documented trace extension exposes these streams before any
        # response exists. httpcore is pinned; the loopback tests exercise this
        # actual hook. Never retain/log request, header or TLS trace metadata.
        if event in {"connection.connect_tcp.complete", "connection.start_tls.complete"}:
            with self.lock:
                self.stream = info.get("return_value")
                if event == "connection.connect_tcp.complete" and self.interrupt_socket is None:
                    # SSL wrapping detaches the raw socket before its handshake
                    # returns. Keep our own handle to the same connection so
                    # shutdown can still wake that handshake. Closing this
                    # duplicate normally does not shut down the live stream.
                    try:
                        connection = self.stream.get_extra_info("socket") if self.stream is not None else None
                    except (AttributeError, OSError, TypeError, ValueError, NotImplementedError):
                        # Injected transports can emit traces without socket
                        # metadata. Preserve their existing close fallback.
                        connection = None
                    if connection is self.connecting_socket:
                        self.connecting_socket = None
                    if isinstance(connection, socket.socket):
                        try:
                            self.interrupt_socket = connection.dup()
                        except OSError:
                            # This trace precedes pool ownership. If handle
                            # allocation fails, explicitly close the unpooled
                            # stream rather than leak it or enter unguarded I/O.
                            self.stream.close()
                            raise AIProviderError("AI_PROVIDER_UNAVAILABLE") from None
            if event == "connection.start_tls.complete":
                # The replacement TLS stream now owns a usable socket. Retain
                # the duplicate only across ownership transfer: on Windows an
                # extra live handle can keep a later body recv blocked on close.
                self.close()
        # Do not raise from response-close traces invoked by our own cleanup.
        if (event.endswith(".started") and ".response_closed." not in event) or event in {
                "connection.connect_tcp.complete", "connection.start_tls.complete"}:
            self.check()

    def bind_response(self, response):
        with self.lock:
            self.response = response
            self.stream = response.extensions.get("network_stream", self.stream)
        # Plain HTTP reaches response ownership without a TLS-complete trace.
        self.close()
        self.check()

    def interrupt(self, code=None):
        with self.lock:
            stream, response = self.stream, self.response
            connection = self.interrupt_socket
            connecting = self.connecting_socket
            self.connecting_socket = None
            self.interrupted = True
            if code is not None and self.interruption_code is None:
                self.interruption_code = code
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if connecting is not None:
            try:
                connecting.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connecting.close()
            except OSError:
                pass
        _shutdown_stream_socket(stream)
        # A connect-complete trace can fire before the pool owns the stream.
        # Close it explicitly too; shutdown alone must not leave an orphan FD.
        for resource in (response, stream, self.client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    # Continue closing other owned resources after a close race.
                    pass

    def close(self):
        with self.lock:
            if self.interrupt_socket is not None:
                self.interrupt_socket.close()
                self.interrupt_socket = None

    def watch(self):
        while not self.finished.is_set():
            try:
                self.check()
            except AIProviderError:
                return
            remaining = max(0, self.deadline - time.monotonic())
            self.finished.wait(min(.01, remaining) if self.cancellation_event is not None else remaining)


@contextmanager
def request_guard(client, *, deadline, cancellation_event=None):
    """Interrupt only this request's connected transport, including header reads.

    The pinned HTTPX/httpcore sync connection pool uses a request-scoped
    backend so DNS waits, each TCP address attempt, TLS and response reads all
    share this absolute deadline. Native getaddrinfo cannot be cancelled; its
    daemon workers are capped process-wide. Custom transports may omit socket
    metadata, so their underlying I/O cannot be certified by this guard. The normal
    response close fallback remains, but cannot certify arbitrary custom I/O.
    """
    guard = _RequestGuard(client, deadline, cancellation_event)
    guard.check()
    transports = [getattr(client, "_transport", None)]
    mounts = getattr(client, "_mounts", None)
    if isinstance(mounts, dict):
        transports.extend(mounts.values())
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        if pool is not None and hasattr(pool, "_network_backend"):
            pool._network_backend = _RequestScopedBackend(guard)
    watcher = threading.Thread(target=guard.watch, name="mjobs-request-deadline", daemon=True)
    watcher.start()
    try:
        try:
            yield guard
        except Exception:
            guard.check()
            raise
        guard.check()
    finally:
        guard.finished.set()
        watcher.join(timeout=1)
        guard.close()
