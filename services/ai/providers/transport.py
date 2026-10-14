"""Request-owned transport cleanup shared by HTTP AI providers."""
import socket
from contextlib import contextmanager
import threading
import time

from services.ai.providers import AIProviderError


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
        self.lock = threading.Lock()
        self.finished = threading.Event()

    def check(self):
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            self.interrupt()
            raise AIProviderError("AI_PROVIDER_CANCELLED")
        if time.monotonic() >= self.deadline:
            self.interrupt()
            raise AIProviderError("AI_PROVIDER_TIMEOUT")

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
                    if isinstance(connection, socket.socket):
                        try:
                            self.interrupt_socket = connection.dup()
                        except OSError:
                            # This trace precedes pool ownership. If handle
                            # allocation fails, explicitly close the unpooled
                            # stream rather than leak it or enter unguarded I/O.
                            self.stream.close()
                            raise AIProviderError("AI_PROVIDER_UNAVAILABLE") from None
        # Do not raise from response-close traces invoked by our own cleanup.
        if (event.endswith(".started") and ".response_closed." not in event) or event in {
                "connection.connect_tcp.complete", "connection.start_tls.complete"}:
            self.check()

    def bind_response(self, response):
        with self.lock:
            self.response = response
            self.stream = response.extensions.get("network_stream", self.stream)
        self.check()

    def interrupt(self):
        with self.lock:
            stream, response = self.stream, self.response
            connection = self.interrupt_socket
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
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

    DNS/TCP establishment retains HTTPX's phase timeouts until a stream
    is exposed. The connected socket remains interruptible through TLS wrapping.
    Custom transports may omit trace/socket metadata. The normal
    response close fallback remains, but cannot certify arbitrary custom I/O.
    """
    guard = _RequestGuard(client, deadline, cancellation_event)
    guard.check()
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
