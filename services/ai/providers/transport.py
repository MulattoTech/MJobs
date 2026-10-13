"""Request-owned transport cleanup shared by HTTP AI providers."""
import socket


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
