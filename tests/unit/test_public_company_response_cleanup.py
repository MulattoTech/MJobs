"""Response cleanup must preserve monitored request outcomes and owned resources."""
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from services.company_discovery.monitoring_policy import MonitoringPolicy
from services.company_discovery.public_client import (
    PublicCompanyClient,
    PublicFetchError,
)
from workers.operations import OperationCancelled, OperationControlUnavailable


class ObservedSocket:
    def __init__(self, close_error=None):
        self.close_calls = 0
        self.close_error = close_error

    def settimeout(self, _timeout):
        pass

    def connect(self, _address):
        pass

    def do_handshake(self):
        pass

    def sendall(self, _wire):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class ClosingResponse:
    status = 200

    def __init__(self, *, read_error=None, close_error=None):
        self.read_error = read_error
        self.close_error = close_error
        self.close_calls = 0
        self.chunks = iter((b"OK", b""))

    def begin(self):
        pass

    def getheaders(self):
        return [("content-length", "2")]

    def read(self, _size):
        if self.read_error is not None:
            raise self.read_error
        return next(self.chunks, b"")

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class PublicCompanyResponseCleanupTests(unittest.TestCase):
    @contextmanager
    def transport(self, response, *, tls_close_error=None):
        raw, tls = ObservedSocket(), ObservedSocket(tls_close_error)
        client = PublicCompanyClient(
            "https://fixture.example/jobs", policy=MonitoringPolicy(request_timeout=2),
        )
        context = Mock()
        context.wrap_socket.return_value = tls
        try:
            with patch.object(client, "_resolve", return_value=[
                    (socket.AF_INET, ("127.0.0.1", 1))]), \
                    patch.object(client, "_wait"), \
                    patch("services.company_discovery.public_client.socket.socket", return_value=raw), \
                    patch("services.company_discovery.public_client.ssl.create_default_context",
                          return_value=context), \
                    patch("services.company_discovery.public_client.http.client.HTTPResponse",
                          return_value=response):
                yield client, raw, tls
        finally:
            client.close()

    def assert_released(self, client, raw, tls, response):
        # Check before client.close(), which would conceal a per-request leak.
        self.assertEqual(client._sockets, set())
        self.assertEqual(raw.close_calls, 1)
        self.assertEqual(tls.close_calls, 1)
        self.assertEqual(response.close_calls, 1)
        self.assertFalse(any(
            thread.name == "mjobs-public-fetch-cancellation"
            for thread in threading.enumerate()
        ))

    def request(self, client):
        return client._request_once(client.website, time.monotonic() + 5)

    def test_successful_body_survives_expected_response_close_failure(self):
        response = ClosingResponse(close_error=OSError("synthetic-private-close-detail"))
        with self.transport(response) as (client, raw, tls):
            result = self.request(client)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content, b"OK")
            self.assert_released(client, raw, tls, response)

    def test_close_failure_cannot_leave_request_sockets_registered(self):
        response = ClosingResponse(close_error=OSError("synthetic-close-failure"))
        with self.transport(response) as (client, raw, tls):
            try:
                self.request(client)
            except OSError:
                pass
            self.assert_released(client, raw, tls, response)

    def test_primary_control_and_policy_failures_survive_expected_close_failure(self):
        for failure in (
            OperationCancelled(),
            OperationControlUnavailable(),
            PublicFetchError("FETCH_DEADLINE"),
            PublicFetchError("RESPONSE_TOO_LARGE"),
            KeyboardInterrupt("synthetic-stop"),
        ):
            with self.subTest(failure=type(failure).__name__):
                response = ClosingResponse(
                    read_error=failure, close_error=OSError("synthetic-private-close-detail"),
                )
                with self.transport(response) as (client, raw, tls):
                    with self.assertRaises(type(failure)) as caught:
                        self.request(client)
                    self.assertIs(caught.exception, failure)
                    self.assert_released(client, raw, tls, response)

    def test_network_failure_keeps_fixed_diagnostic_when_close_also_fails(self):
        response = ClosingResponse(
            read_error=OSError("synthetic-private-read-detail"),
            close_error=OSError("synthetic-private-close-detail"),
        )
        with self.transport(response) as (client, raw, tls):
            with self.assertRaises(PublicFetchError) as caught:
                self.request(client)
            self.assertEqual(caught.exception.code, "NETWORK_FAILED")
            self.assertEqual(str(caught.exception), "NETWORK_FAILED")
            self.assert_released(client, raw, tls, response)

    def test_unexpected_response_close_failure_still_disposes_owned_sockets(self):
        failure = RuntimeError("synthetic-unexpected-close")
        response = ClosingResponse(close_error=failure)
        with self.transport(response) as (client, raw, tls):
            with self.assertRaises(RuntimeError) as caught:
                self.request(client)
            self.assertIs(caught.exception, failure)
            self.assert_released(client, raw, tls, response)

    def test_socket_close_error_does_not_skip_the_other_owned_socket(self):
        response = ClosingResponse(close_error=OSError("synthetic-response-close"))
        with self.transport(response, tls_close_error=OSError("synthetic-tls-close")) as (
                client, raw, tls):
            self.assertEqual(self.request(client).content, b"OK")
            self.assert_released(client, raw, tls, response)


if __name__ == "__main__":
    unittest.main()
