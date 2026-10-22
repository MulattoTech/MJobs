"""Small response fragments must not delay Remotive deadline checks."""
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from services.discovery.sources.remotive import Remotive
from workers.operations import OperationTimedOut


class RemotiveBodyDeadlineTests(unittest.TestCase):
    def test_deadline_does_not_wait_for_blocked_cancellation_watcher_close(self):
        watcher_close_started = threading.Event()
        release_watcher_close = threading.Event()
        deadline = time.monotonic() + 0.15

        class SlowCloseBody(httpx.SyncByteStream):
            def __iter__(self):
                while True:
                    yield b" " * 8

            def close(self):
                if threading.current_thread().name == "mjobs-remotive-cancellation":
                    watcher_close_started.set()
                    release_watcher_close.wait(2)

        body = SlowCloseBody()

        def check():
            if time.monotonic() >= deadline:
                if threading.current_thread().name != "mjobs-remotive-cancellation":
                    watcher_close_started.wait(0.1)
                raise OperationTimedOut("OPERATION_TIMEOUT")

        check.remaining_seconds = lambda: 2.0
        adapter = Remotive(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=body)),
            check=check,
            request_timeout=2,
        )

        with patch("services.discovery.sources.remotive.CANCELLATION_POLL_INTERVAL", 0.01):
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(OperationTimedOut, "OPERATION_TIMEOUT"):
                    adapter.search()
                elapsed = time.monotonic() - started
                self.assertTrue(watcher_close_started.wait(0.1))
                self.assertLess(elapsed, 0.4, "deadline cleanup waited on the watcher close")
            finally:
                release_watcher_close.set()

    def test_worker_cancellation_is_checked_between_small_body_fragments(self):
        class FragmentedBody(httpx.SyncByteStream):
            def __init__(self):
                self.fragments = 0
                self.closed = False

            def __iter__(self):
                while True:
                    self.fragments += 1
                    yield b" " * 8

            def close(self):
                self.closed = True

        body = FragmentedBody()

        def check():
            if body.fragments >= 5:
                raise OperationTimedOut("OPERATION_TIMEOUT")

        check.remaining_seconds = lambda: 0.8
        client_transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=body)
        )
        adapter = Remotive(transport=client_transport, check=check, request_timeout=2)

        with patch("services.discovery.sources.remotive.CANCELLATION_POLL_INTERVAL", 60):
            with self.assertRaisesRegex(OperationTimedOut, "OPERATION_TIMEOUT"):
                adapter.search()

        self.assertEqual(body.fragments, 5)
        self.assertTrue(body.closed)

    def test_valid_json_body_is_parsed_when_split_into_small_fragments(self):
        class FragmentedBody(httpx.SyncByteStream):
            def __init__(self):
                self.fragments = 0

            def __iter__(self):
                for fragment in (b'{"jobs":', b"[", b"]", b"}"):
                    self.fragments += 1
                    yield fragment

            def close(self):
                pass

        body = FragmentedBody()
        checks = []

        def check():
            checks.append(True)

        check.remaining_seconds = lambda: 0.8
        adapter = Remotive(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, stream=body)
            ),
            check=check,
            request_timeout=2,
        )

        with patch("services.discovery.sources.remotive.CANCELLATION_POLL_INTERVAL", 60):
            self.assertEqual(adapter._request(), {"jobs": []})

        self.assertEqual(body.fragments, 4)
        self.assertGreaterEqual(len(checks), body.fragments + 2)
        self.assertEqual(adapter.health(), "HEALTHY")


    def test_cancellation_closes_client_before_blocked_response_cleanup(self):
        release_body = threading.Event()
        body_read_waiting = threading.Event()
        response_close_started = threading.Event()
        client_closed = threading.Event()
        close_order = []
        deadline = time.monotonic() + 0.15

        class BlockingBody(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{"jobs":'
                body_read_waiting.set()
                release_body.wait(1.5)
                yield b'[]}'

            def close(self):
                close_order.append("response")
                response_close_started.set()
                release_body.wait(1.5)

        class BlockingTransport(httpx.BaseTransport):
            def handle_request(self, _request):
                return httpx.Response(200, stream=BlockingBody())

            def close(self):
                close_order.append("client")
                client_closed.set()
                release_body.set()

        def check():
            if time.monotonic() >= deadline:
                raise OperationTimedOut("OPERATION_TIMEOUT")

        check.remaining_seconds = lambda: max(0.001, deadline - time.monotonic())
        adapter = Remotive(transport=BlockingTransport(), check=check, request_timeout=2)

        with patch("services.discovery.sources.remotive.CANCELLATION_POLL_INTERVAL", 0.01):
            started = time.monotonic()
            with self.assertRaisesRegex(OperationTimedOut, "OPERATION_TIMEOUT"):
                adapter.search()
            elapsed = time.monotonic() - started

        self.assertTrue(body_read_waiting.is_set())
        self.assertTrue(response_close_started.is_set())
        self.assertTrue(client_closed.is_set())
        self.assertEqual(close_order[:2], ["client", "response"])
        self.assertLess(elapsed, 0.4, "response cleanup delayed cancellation past the deadline")


if __name__ == "__main__":
    unittest.main()
