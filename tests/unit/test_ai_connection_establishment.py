"""AI provider connection setup obeys the request deadline and cancellation."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import errno
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from services.ai.providers import AIProviderError, OpenAICompatibleProvider


class PendingSocket:
    instances = []
    connected = threading.Event()
    on_duplicate = None
    connect_delay = 0
    connect_status = getattr(errno, "WSAEWOULDBLOCK", errno.EINPROGRESS)
    connect_callback = None

    def __init__(self, *_args):
        self.closed = False
        type(self).instances.append(self)

    def setblocking(self, _value):
        pass

    def bind(self, _address):
        pass

    def connect_ex(self, _address):
        type(self).connected.set()
        if type(self).connect_callback is not None:
            return type(self).connect_callback(_address)
        if type(self).connect_delay:
            time.sleep(type(self).connect_delay)
        return type(self).connect_status

    def getsockopt(self, *_args):
        return 0

    def setsockopt(self, *_args):
        pass

    def shutdown(self, *_args):
        pass

    def close(self):
        self.closed = True

    def dup(self):
        duplicate = type(self)()
        if type(self).on_duplicate is not None:
            type(self).on_duplicate()
        return duplicate

    def fileno(self):
        return -1 if self.closed else 123


class AIConnectionEstablishmentTests(unittest.TestCase):
    def make_provider(self, url, client_factory=None):
        return OpenAICompatibleProvider(
            base_url=url,
            api_key="synthetic-secret",
            client_factory=client_factory or httpx.Client,
        )

    def provider_result(self, provider, *, timeout, cancellation=None):
        try:
            return provider.generate(
                task="test",
                model="fixture",
                prompt="synthetic-private-prompt",
                timeout_seconds=timeout,
                cancellation_event=cancellation,
            )
        except AIProviderError as error:
            return error.code

    def stalled_dns(self, host):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def resolve(name, port, *args, **kwargs):
            if name != host:
                return socket.getaddrinfo(name, port, *args, **kwargs)
            entered.set()
            try:
                release.wait(5)
                return []
            finally:
                finished.set()

        return entered, release, finished, resolve

    def reset_pending_sockets(self):
        PendingSocket.instances = []
        PendingSocket.connected = threading.Event()
        PendingSocket.on_duplicate = None
        PendingSocket.connect_delay = 0
        PendingSocket.connect_status = getattr(errno, "WSAEWOULDBLOCK", errno.EINPROGRESS)
        PendingSocket.connect_callback = None

    def fake_resolution(self, _host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.44", port))]

    def make_pending(self, *, select_result):
        self.reset_pending_sockets()
        def wait_for_connect(_read, write, _exception, timeout):
            time.sleep(min(timeout, .01))
            return select_result(write)
        return wait_for_connect

    def test_absolute_deadline_interrupts_blocked_dns_without_leaking_request_or_resolver(self):
        host = "deadline.synthetic.invalid"
        entered, release, finished, resolve = self.stalled_dns(host)
        clients = []

        def factory(**kwargs):
            client = httpx.Client(**kwargs)
            clients.append(client)
            return client

        provider = self.make_provider(f"https://{host}/v1", factory)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=resolve):
                started = time.monotonic()
                future = pool.submit(self.provider_result, provider, timeout=1.2)
                self.assertTrue(entered.wait(1), "DNS did not start")
                try:
                    result = future.result(timeout=2)
                except FutureTimeout:
                    self.fail("absolute request deadline did not interrupt DNS wait")
                self.assertLess(time.monotonic() - started, 1.8,
                                "DNS wait exceeded the absolute operation deadline")
            self.assertEqual(result, "AI_PROVIDER_TIMEOUT")
            self.assertTrue(clients[0].is_closed)
            self.assertNotIn("synthetic-secret", result)
            self.assertNotIn("synthetic-private-prompt", result)
        finally:
            release.set()
            pool.shutdown(wait=True, cancel_futures=True)
        self.assertTrue(finished.wait(1), "bounded DNS resolver did not finish after release")
        self.assertFalse(any(t.name == "mjobs-request-deadline" for t in threading.enumerate()))
        self.assertFalse(any(t.name == "mjobs-dns-resolver" for t in threading.enumerate()))

    def test_cancellation_interrupts_blocked_dns_and_is_not_classified_as_timeout(self):
        host = "cancel.synthetic.invalid"
        entered, release, finished, resolve = self.stalled_dns(host)
        clients = []

        def factory(**kwargs):
            client = httpx.Client(**kwargs)
            clients.append(client)
            return client

        provider = self.make_provider(f"https://{host}/v1", factory)
        cancellation = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=resolve):
                future = pool.submit(self.provider_result, provider, timeout=4, cancellation=cancellation)
                self.assertTrue(entered.wait(1), "DNS did not start")
                cancellation.set()
                try:
                    result = future.result(timeout=0.7)
                except FutureTimeout:
                    self.fail("cancellation did not interrupt DNS wait")
            self.assertEqual(result, "AI_PROVIDER_CANCELLED")
            self.assertTrue(clients[0].is_closed)
        finally:
            release.set()
            pool.shutdown(wait=True, cancel_futures=True)
        self.assertTrue(finished.wait(1), "bounded DNS resolver did not finish after release")
        self.assertFalse(any(t.name == "mjobs-request-deadline" for t in threading.enumerate()))
        self.assertFalse(any(t.name == "mjobs-dns-resolver" for t in threading.enumerate()))

    def test_stalled_dns_worker_count_is_bounded_under_concurrent_cancellation(self):
        lock = threading.Lock()
        release = threading.Event()
        four_started = threading.Event()
        all_finished = threading.Event()
        active = 0
        peak = 0
        finished = 0

        def resolve(_host, port, *args, **kwargs):
            nonlocal active, peak, finished
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    four_started.set()
            try:
                release.wait(5)
                return []
            finally:
                with lock:
                    active -= 1
                    finished += 1
                    if finished == 4:
                        all_finished.set()

        cancellations = [threading.Event() for _ in range(6)]
        providers = [self.make_provider(f"https://bounded-{index}.synthetic.invalid/v1")
                     for index in range(6)]
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=resolve):
                futures = [pool.submit(self.provider_result, provider, timeout=4, cancellation=event)
                           for provider, event in zip(providers, cancellations)]
                self.assertTrue(four_started.wait(2), "DNS workers did not reach the configured cap")
                for event in cancellations:
                    event.set()
                for future in futures:
                    self.assertEqual(future.result(timeout=1), "AI_PROVIDER_CANCELLED")
                self.assertEqual(peak, 4)
                self.assertLessEqual(sum(t.name == "mjobs-dns-resolver" for t in threading.enumerate()), 4)
        finally:
            release.set()
            pool.shutdown(wait=True, cancel_futures=True)
        self.assertTrue(all_finished.wait(1), "bounded DNS workers did not exit after release")
        self.assertFalse(any(t.name == "mjobs-dns-resolver" for t in threading.enumerate()))

    def test_cancelled_before_request_does_not_start_dns_or_create_client(self):
        cancellation = threading.Event()
        cancellation.set()
        factory = unittest.mock.Mock()
        provider = self.make_provider("https://pre-cancel.synthetic.invalid/v1", factory)

        self.assertEqual(self.provider_result(provider, timeout=2, cancellation=cancellation),
                         "AI_PROVIDER_CANCELLED")
        factory.assert_not_called()

    def test_cancellation_closes_a_pending_tcp_socket_promptly(self):
        cancellation = threading.Event()
        provider = self.make_provider("https://pending.synthetic.invalid/v1")
        pending = self.make_pending(select_result=lambda _write: ([], [], []))
        with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=self.fake_resolution), \
                patch("services.ai.providers.transport.socket.socket", PendingSocket), \
                patch("services.ai.providers.transport.select.select", side_effect=pending):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.provider_result, provider, timeout=4, cancellation=cancellation)
                self.assertTrue(PendingSocket.connected.wait(1), "TCP connect did not start")
                before = time.monotonic()
                cancellation.set()
                self.assertEqual(future.result(timeout=1), "AI_PROVIDER_CANCELLED")
                self.assertLess(time.monotonic() - before, .6)
        self.assertEqual(len(PendingSocket.instances), 1)
        self.assertTrue(PendingSocket.instances[0].closed, "pending TCP socket leaked")

    def test_absolute_deadline_is_shared_across_bounded_address_attempts(self):
        self.reset_pending_sockets()
        provider = self.make_provider("https://attempts.synthetic.invalid/v1")
        attempts = []
        def addresses(_host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (f"192.0.2.{n}", port))
                    for n in range(20, 40)]
        def fail_after_delay(_address):
            attempts.append(time.monotonic())
            time.sleep(.38)
            return errno.ECONNREFUSED
        PendingSocket.connect_callback = fail_after_delay
        with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=addresses), \
                patch("services.ai.providers.transport.socket.socket", PendingSocket):
            started = time.monotonic()
            result = self.provider_result(provider, timeout=1)
            elapsed = time.monotonic() - started
        self.assertEqual(result, "AI_PROVIDER_TIMEOUT")
        self.assertLess(elapsed, 1.4, "each address attempt received a fresh timeout")
        self.assertGreaterEqual(len(attempts), 2, "the deadline was not tested across attempts")
        self.assertLessEqual(len(attempts), 3, "connection retries exceeded the shared deadline")
        self.assertTrue(all(sock.closed for sock in PendingSocket.instances))

    def test_address_failover_is_capped(self):
        self.reset_pending_sockets()
        provider = self.make_provider("https://cap.synthetic.invalid/v1")
        attempts = []
        def addresses(_host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (f"192.0.2.{n}", port))
                    for n in range(20, 40)]
        def refuse(_address):
            attempts.append(True)
            return errno.ECONNREFUSED
        PendingSocket.connect_callback = refuse
        with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=addresses), \
                patch("services.ai.providers.transport.socket.socket", PendingSocket):
            result = self.provider_result(provider, timeout=2)
        self.assertEqual(result, "AI_PROVIDER_UNAVAILABLE")
        self.assertEqual(len(attempts), 8)
        self.assertTrue(all(sock.closed for sock in PendingSocket.instances))

    def test_socket_creation_failure_falls_back_to_next_dns_address(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        original_socket = socket.socket
        unsupported_family = 999
        attempted_families = []

        class SelectiveSocket(original_socket):
            def __new__(cls, family=socket.AF_INET, type=socket.SOCK_STREAM,
                        proto=0, fileno=None):
                attempted_families.append(family)
                if family == unsupported_family:
                    raise OSError(errno.EAFNOSUPPORT, "synthetic unsupported address family")
                return original_socket.__new__(cls, family, type, proto, fileno)

        def addresses(_host, port, *args, **kwargs):
            return [
                (unsupported_family, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port)),
                *original_socket_getaddrinfo("127.0.0.1", port, type=socket.SOCK_STREAM),
            ]

        original_socket_getaddrinfo = socket.getaddrinfo
        try:
            with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=addresses), \
                    patch("services.ai.providers.transport.socket.socket", SelectiveSocket):
                provider = self.make_provider(
                    f"http://127.0.0.1:{server.server_port}/v1")
                result = self.provider_result(provider, timeout=2)
            self.assertEqual(result, "ok", attempted_families)
            self.assertEqual(attempted_families[:2], [unsupported_family, socket.AF_INET])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "loopback server thread leaked")

    def test_cancellation_racing_with_tcp_completion_closes_both_socket_handles(self):
        cancellation = threading.Event()
        provider = self.make_provider("https://race.synthetic.invalid/v1")
        pending = self.make_pending(select_result=lambda write: ([], write, []))
        PendingSocket.connect_status = 0
        PendingSocket.on_duplicate = cancellation.set
        with patch("services.ai.providers.transport.socket.getaddrinfo", side_effect=self.fake_resolution), \
                patch("services.ai.providers.transport.socket.socket", PendingSocket), \
                patch("services.ai.providers.transport.select.select", side_effect=pending):
            result = self.provider_result(provider, timeout=4, cancellation=cancellation)
        self.assertEqual(result, "AI_PROVIDER_CANCELLED")
        self.assertEqual(len(PendingSocket.instances), 2)
        self.assertTrue(all(sock.closed for sock in PendingSocket.instances), "completion race leaked a socket")

    def test_synthetic_loopback_connection_still_completes(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            provider = self.make_provider(f"http://127.0.0.1:{server.server_port}/v1")
            self.assertEqual(self.provider_result(provider, timeout=2), "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "loopback server thread leaked")
        self.assertFalse(any(t.name == "mjobs-request-deadline" for t in threading.enumerate()))
