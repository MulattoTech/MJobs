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
    try:
        stream = response.extensions.get("network_stream")
        connection = stream.get_extra_info("socket") if stream is not None else None
        if isinstance(connection, socket.socket):
            connection.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError, TypeError, ValueError, NotImplementedError):
        # A concurrent close or a transport without socket metadata is harmless.
        return


@contextmanager
def response_deadline(response, *, deadline):
    """Bound body consumption even if each read receives bytes before its timeout.

    The caller's monotonic deadline includes time already spent obtaining headers.
    Before headers arrive, HTTPX's connect/write/read timeouts still apply. Only
    this response's socket is interrupted; its client never reuses the connection.
    """
    finished, expired = threading.Event(), threading.Event()

    def interrupt_at_deadline():
        if finished.wait(max(0, deadline - time.monotonic())):
            return
        expired.set()
        _shutdown_response_socket(response)
        try:
            response.close()
        except Exception:
            # A concurrent cancellation/close is harmless; timeout is authoritative.
            pass

    watcher = threading.Thread(target=interrupt_at_deadline,
                               name='mjobs-response-deadline', daemon=True)
    watcher.start()
    try:
        try:
            yield
        except Exception:
            if expired.is_set() or time.monotonic() >= deadline:
                raise AIProviderError('AI_PROVIDER_TIMEOUT') from None
            raise
        if expired.is_set() or time.monotonic() >= deadline:
            raise AIProviderError('AI_PROVIDER_TIMEOUT')
    finally:
        finished.set()
        watcher.join(timeout=1)
