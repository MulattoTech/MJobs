"""Bounded public-company HTTPS transport policy tests."""
import socket
import time
import unittest
from unittest.mock import patch

import httpx

from services.company_discovery.public_client import (
    MAX_RESPONSE_BYTES, HTTPStatusError, PublicCompanyClient, PublicFetchError,
)


def response(status, *, headers=None, url="https://example.test/jobs"):
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("GET", url))


class FakeHttpResponse:
    status = 200

    def __init__(self, chunks, headers=None):
        self.chunks = iter(chunks)
        self.headers = headers or [("content-type", "text/plain")]

    def begin(self):
        return None

    def getheaders(self):
        return self.headers

    def read(self, _size):
        return next(self.chunks, b"")


class PublicCompanyClientTests(unittest.TestCase):
    def test_unsafe_redirect_is_rejected_before_second_request(self):
        client = PublicCompanyClient("https://example.test/jobs")
        try:
            with patch.object(client, "_load_robots"), patch.object(client, "_wait"), patch.object(
                    client, "_request_once", return_value=response(302, headers={"location": "https://127.0.0.1/private"})) as request:
                with self.assertRaisesRegex(PublicFetchError, "REDIRECT_BLOCKED"):
                    client.get("https://example.test/jobs")
            request.assert_called_once()
        finally:
            client.close()

    def test_redirect_target_is_checked_against_robots_before_egress(self):
        client = PublicCompanyClient("https://example.test/jobs")
        first = response(302, headers={"location": "https://example.test/private"})
        try:
            with patch.object(client, "_load_robots"), patch.object(client, "_wait"), patch.object(
                    client, "_request_once", return_value=first) as request:
                client._robots = type("Robots", (), {"can_fetch": lambda self, _agent, url: not url.endswith("/private")})()
                with self.assertRaisesRegex(PublicFetchError, "ROBOTS_DENIED"):
                    client.get("https://example.test/jobs")
            request.assert_called_once()
        finally:
            client.close()

    def test_dns_resolution_is_single_pinned_lookup_and_rejects_rebinding_answer(self):
        client = PublicCompanyClient("https://example.test/jobs")
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        try:
            with patch("services.company_discovery.public_client.socket.getaddrinfo",
                       side_effect=[records, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]]) as lookup:
                self.assertEqual(client._resolve("example.test", 443)[0][1][0], "8.8.8.8")
                self.assertEqual(lookup.call_count, 1)
        finally:
            client.close()

    def test_private_dns_answer_is_fail_closed(self):
        client = PublicCompanyClient("https://example.test/jobs")
        try:
            with patch("services.company_discovery.public_client.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))]):
                with self.assertRaisesRegex(PublicFetchError, "DNS_PRIVATE"):
                    client._resolve("example.test", 443)
        finally:
            client.close()

    def test_only_first_validated_dns_destination_is_attempted(self):
        client = PublicCompanyClient("https://example.test/jobs")
        class Socket:
            def settimeout(self, _timeout): pass
            def connect(self, _address): raise OSError("connect failed")
            def close(self): pass
        try:
            with patch.object(client, "_resolve", return_value=[
                    (socket.AF_INET, ("8.8.8.8", 443)), (socket.AF_INET, ("1.1.1.1", 443))]), \
                 patch.object(client, "_wait"), patch("services.company_discovery.public_client.socket.socket", return_value=Socket()) as factory:
                with self.assertRaisesRegex(PublicFetchError, "NETWORK_FAILED"):
                    client._request_once("https://example.test/jobs", __import__("time").monotonic() + 10)
            self.assertEqual(factory.call_count, 1)
        finally:
            client.close()

    def test_cgnat_dns_answer_is_not_global(self):
        client = PublicCompanyClient("https://example.test/jobs")
        try:
            with patch("services.company_discovery.public_client.socket.getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))]):
                with self.assertRaisesRegex(PublicFetchError, "DNS_NOT_GLOBAL"):
                    client._resolve("example.test", 443)
        finally:
            client.close()

    def test_tls_uses_original_hostname_when_connecting_numeric_address(self):
        client = PublicCompanyClient("https://Example.test/jobs")
        class FakeTLS:
            def __init__(self): self.wire = None
            def settimeout(self, _timeout): pass
            def sendall(self, wire): self.wire = wire
            def close(self): pass
        class FakeContext:
            def __init__(self): self.server_hostname = None; self.tls = FakeTLS()
            def wrap_socket(self, sock, *, server_hostname):
                self.server_hostname = server_hostname
                return self.tls
        context = FakeContext()
        class FakeSocket:
            def settimeout(self, _timeout): pass
            def connect(self, _address): pass
            def close(self): pass
        try:
            with patch.object(client, "_resolve", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]), \
                 patch.object(client, "_wait"), patch("services.company_discovery.public_client.socket.socket", return_value=FakeSocket()) as socket_factory, \
                 patch("services.company_discovery.public_client.ssl.create_default_context", return_value=context), \
                 patch("services.company_discovery.public_client.http.client.HTTPResponse", return_value=FakeHttpResponse([b"ok"])):
                result = client._request_once("https://Example.test/jobs", __import__("time").monotonic() + 10)
            self.assertEqual(result.content, b"ok")
            self.assertEqual(context.server_hostname, "example.test")
            self.assertEqual(socket_factory.call_args.args[0], socket.AF_INET)
            self.assertIn(b"/jobs HTTP/1.1", context.tls.wire or b"")
        finally:
            client.close()

    def test_truncated_or_ambiguous_length_cannot_be_a_successful_response(self):
        class TLS:
            def settimeout(self, timeout): pass
            def connect(self, address): pass
            def sendall(self, wire): pass
            def close(self): pass
        for headers in [[('content-length','10')], [('content-length','1'),('Content-Length','2')]]:
            client=PublicCompanyClient('https://example.test/jobs')
            try:
                with patch.object(client,'_resolve',return_value=[(socket.AF_INET,('8.8.8.8',443))]), \
                     patch.object(client,'_wait'), patch('services.company_discovery.public_client.socket.socket',return_value=TLS()), \
                     patch('services.company_discovery.public_client.ssl.create_default_context') as context, \
                     patch('services.company_discovery.public_client.http.client.HTTPResponse',return_value=FakeHttpResponse([b'x'],headers)):
                    context.return_value.wrap_socket.return_value=TLS()
                    with self.assertRaisesRegex(PublicFetchError,'RESPONSE_LENGTH_INVALID'):
                        client._request_once('https://example.test/jobs',time.monotonic()+10)
            finally:client.close()

    def test_expired_client_lifetime_prevents_more_network_requests(self):
        client=PublicCompanyClient('https://example.test/jobs')
        client._deadline=time.monotonic()-1
        try:
            with patch.object(client,'_request_once',side_effect=AssertionError('No request after deadline')):
                with self.assertRaisesRegex(PublicFetchError,'FETCH_DEADLINE'):
                    client.get('https://example.test/jobs')
        finally:client.close()

    def test_streaming_response_limit_fails_closed(self):
        client = PublicCompanyClient("https://example.test/jobs")
        try:
            chunks = [b"x" * MAX_RESPONSE_BYTES, b"y"]
            with patch.object(client, "_resolve", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]), \
                 patch.object(client, "_wait"), patch("services.company_discovery.public_client.socket.socket", return_value=type("Socket", (), {
                     "settimeout": lambda self, _timeout: None,
                     "connect": lambda self, _address: None,
                     "close": lambda self: None,
                 })()), \
                 patch("services.company_discovery.public_client.ssl.create_default_context") as context_factory:
                context_factory.return_value.wrap_socket.return_value = FakeTLS = type("TLS", (), {
                    "settimeout": lambda self, _timeout: None,
                    "sendall": lambda self, _wire: None,
                    "close": lambda self: None,
                })()
                with patch("services.company_discovery.public_client.http.client.HTTPResponse", return_value=FakeHttpResponse(chunks)):
                    with self.assertRaisesRegex(PublicFetchError, "RESPONSE_TOO_LARGE"):
                        client._request_once("https://example.test/jobs", __import__("time").monotonic() + 10)
        finally:
            client.close()

    def test_content_encoding_and_header_size_are_rejected_before_response_adapter(self):
        client = PublicCompanyClient("https://example.test/jobs")
        try:
            class TLS:
                def settimeout(self, _timeout): pass
                def connect(self, _address): pass
                def sendall(self, _wire): pass
                def close(self): pass
            with patch.object(client, "_resolve", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]), \
                 patch.object(client, "_wait"), patch("services.company_discovery.public_client.socket.socket", return_value=TLS()), \
                 patch("services.company_discovery.public_client.ssl.create_default_context") as context_factory, \
                 patch("services.company_discovery.public_client.http.client.HTTPResponse", return_value=FakeHttpResponse([b"body"], [("content-encoding", "gzip")])):
                context_factory.return_value.wrap_socket.return_value = TLS()
                with self.assertRaisesRegex(PublicFetchError, "CONTENT_ENCODING_UNSUPPORTED"):
                    client._request_once("https://example.test/jobs", __import__("time").monotonic() + 10)
        finally:
            client.close()
        duplicate = PublicCompanyClient("https://example.test/jobs")
        try:
            class TLS:
                def settimeout(self, _timeout): pass
                def connect(self, _address): pass
                def sendall(self, _wire): pass
                def close(self): pass
            with patch.object(duplicate, "_resolve", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]), \
                 patch.object(duplicate, "_wait"), patch("services.company_discovery.public_client.socket.socket", return_value=TLS()), \
                 patch("services.company_discovery.public_client.ssl.create_default_context") as context_factory, \
                 patch("services.company_discovery.public_client.http.client.HTTPResponse", return_value=FakeHttpResponse(
                     [b"body"], [("content-encoding", "identity"), ("content-encoding", "gzip")])):
                context_factory.return_value.wrap_socket.return_value = TLS()
                with self.assertRaisesRegex(PublicFetchError, "CONTENT_ENCODING_UNSUPPORTED"):
                    duplicate._request_once("https://example.test/jobs", __import__("time").monotonic() + 10)
        finally:
            duplicate.close()

    def test_percent_encoded_target_is_sent_as_raw_url_bytes(self):
        client = PublicCompanyClient("https://example.test/jobs")
        class TLS:
            def __init__(self): self.wire = b""
            def settimeout(self, _timeout): pass
            def connect(self, _address): pass
            def sendall(self, wire): self.wire = wire
            def close(self): pass
        context = type("Context", (), {})()
        context.tls = TLS()
        context.wrap_socket = lambda _sock, server_hostname: context.tls
        try:
            with patch.object(client, "_resolve", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]), \
                 patch.object(client, "_wait"), patch("services.company_discovery.public_client.socket.socket", return_value=TLS()), \
                 patch("services.company_discovery.public_client.ssl.create_default_context", return_value=context), \
                 patch("services.company_discovery.public_client.http.client.HTTPResponse", return_value=FakeHttpResponse([b"ok"])):
                client._request_once("https://example.test/jobs/%E2%9C%93?q=%C3%A9", __import__("time").monotonic() + 10)
            self.assertIn(b"/jobs/%E2%9C%93?q=%C3%A9 HTTP/1.1", context.tls.wire)
        finally:
            client.close()

    def test_slow_trickling_body_cannot_cross_absolute_deadline(self):
        client = PublicCompanyClient("https://example.test/jobs")
        class SlowResponse(FakeHttpResponse):
            def read(self, _size):
                time.sleep(0.02)
                return super().read(_size)
        class TLS:
            def settimeout(self, _timeout): pass
            def connect(self, _address): pass
            def sendall(self, _wire): pass
            def shutdown(self, _how): pass
            def close(self): pass
        try:
            with patch.object(client, "_resolve", return_value=[(socket.AF_INET, ("8.8.8.8", 443))]), \
                 patch.object(client, "_wait"), patch("services.company_discovery.public_client.socket.socket", return_value=TLS()), \
                 patch("services.company_discovery.public_client.ssl.create_default_context") as context_factory, \
                 patch("services.company_discovery.public_client.http.client.HTTPResponse", return_value=SlowResponse([b"still waiting"])):
                context_factory.return_value.wrap_socket.return_value = TLS()
                with self.assertRaisesRegex(PublicFetchError, "FETCH_DEADLINE"):
                    client._request_once("https://example.test/jobs", time.monotonic() + 0.005)
        finally:
            client.close()

    def test_cancellation_callback_exception_is_preserved(self):
        class OperationCancelled(Exception):
            pass
        error = OperationCancelled("stop")
        client = PublicCompanyClient("https://example.test/jobs", check=lambda: (_ for _ in ()).throw(error))
        try:
            with self.assertRaises(OperationCancelled) as caught:
                client._check()
            self.assertIs(caught.exception, error)
        finally:
            client.close()

    def test_robots_404_allows_and_403_fails_closed(self):
        allowed = PublicCompanyClient("https://example.test/jobs")
        try:
            with patch.object(allowed, "_request_once", side_effect=[response(404), response(200)]) as request:
                self.assertEqual(allowed.get("https://example.test/jobs").status_code, 200)
                self.assertEqual(request.call_count, 2)
        finally:
            allowed.close()
        denied = PublicCompanyClient("https://example.test/jobs")
        try:
            with patch.object(denied, "_request_once", return_value=response(403)):
                with self.assertRaisesRegex(PublicFetchError, "ROBOTS_DENIED"):
                    denied.get("https://example.test/jobs")
        finally:
            denied.close()

    def test_final_http_statuses_raise_safe_typed_errors(self):
        for status, code in ((401, "HTTP_STATUS_BLOCKED"), (403, "HTTP_STATUS_BLOCKED"),
                             (429, "HTTP_STATUS_BLOCKED"), (500, "HTTP_STATUS_ERROR")):
            client = PublicCompanyClient("https://example.test/jobs")
            try:
                with patch.object(client, "_load_robots"), patch.object(client, "_request_once", return_value=response(status)):
                    with self.assertRaises(HTTPStatusError) as raised:
                        client.get("https://example.test/jobs")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.status_code, status)
            finally:
                client.close()

    def test_unsupported_or_locationless_redirects_fail_closed(self):
        for redirect in (response(300, headers={"location": "https://example.test/next"}), response(302)):
            client = PublicCompanyClient("https://example.test/jobs")
            try:
                with patch.object(client, "_load_robots"), patch.object(client, "_request_once", return_value=redirect):
                    expected = "HTTP_STATUS_ERROR" if redirect.status_code == 300 else "REDIRECT_LOCATION_MISSING"
                    with self.assertRaisesRegex(PublicFetchError, expected):
                        client.get("https://example.test/jobs")
            finally:
                client.close()

    def test_ipv6_robots_url_is_bracketed(self):
        client = PublicCompanyClient("https://[2001:4860:4860::8888]/jobs")
        try:
            with patch.object(client, "_request_once", side_effect=[response(404, url="https://[2001:4860:4860::8888]/robots.txt"), response(200)]) as request:
                client.get("https://[2001:4860:4860::8888]/jobs")
            self.assertIn("https://[2001:4860:4860::8888]/robots.txt", request.call_args_list[0].args)
        finally:
            client.close()

    def test_no_credentials_fragments_or_cross_origin_urls_are_allowed(self):
        client = PublicCompanyClient("https://example.test/jobs")
        try:
            for url in ("https://user:pass@example.test/jobs", "https://example.test/jobs#frag",
                        "http://example.test/jobs", "https://other.test/jobs", "https://example.test/\n",
                        "https://example.test:444/jobs", "https://example.test\\@other.test/jobs",
                        "https://" + "a" * 2001 + ".test/jobs"):
                self.assertFalse(client.allowed(url), url)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
