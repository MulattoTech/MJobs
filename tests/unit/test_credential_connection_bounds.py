"""Boundary cases supplementing the real socket credential probe regressions."""
import threading
import time
from unittest.mock import patch

import httpx

from services.credentials import connection


def test_pre_cancelled_probe_never_sends_authentication():
    cancelled = threading.Event()
    cancelled.set()
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)))
    result = connection.test_provider_connection(
        "openai", "fictional-probe-secret", client_factory=lambda **_kwargs: client,
        cancellation_event=cancelled)
    assert result == {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_CANCELLED"}
    assert calls == []
    assert client.is_closed


def test_deadline_expired_before_worker_start_never_sends_authentication():
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)))
    result = connection.test_provider_connection(
        "openai", "fictional-probe-secret", client_factory=lambda **_kwargs: client,
        deadline=time.monotonic() - 1)
    assert result == {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_TIMEOUT"}
    assert calls == []
    assert client.is_closed


def test_client_initialization_consumes_total_budget_before_request():
    ticks = [100.0]
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)))

    def factory(**_kwargs):
        ticks[0] += 9
        return client

    with patch.object(connection.time, "monotonic", side_effect=lambda: ticks[0]):
        result = connection.test_provider_connection(
            "anthropic", "fictional-probe-secret", client_factory=factory)
    assert result == {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_TIMEOUT"}
    assert calls == []
    assert client.is_closed


def test_response_body_is_never_iterated_or_decompressed(caplog):
    class ForbiddenBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            raise AssertionError("Unused response body must not be read")

        def close(self):
            self.closed = True

    body = ForbiddenBody()
    client = httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)))
    result = connection.test_provider_connection(
        "openai", "fictional-probe-secret", client_factory=lambda **_kwargs: client)
    assert result == {"status": "CONNECTED", "code": None}
    assert body.closed and client.is_closed
    assert "fictional-probe-secret" not in caplog.text


def test_transport_exception_text_is_not_returned_or_logged(caplog):
    def handler(_request):
        raise httpx.ConnectError("fictional-probe-secret reflected by failing transport")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = connection.test_provider_connection(
        "anthropic", "fictional-probe-secret", client_factory=lambda **_kwargs: client)
    assert result == {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNAVAILABLE"}
    assert client.is_closed
    assert "fictional-probe-secret" not in repr(result) + caplog.text
