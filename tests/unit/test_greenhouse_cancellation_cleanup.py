"""Greenhouse cancellation cleanup must not extend the worker boundary."""
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from services.discovery.sources import greenhouse as greenhouse_module
from services.discovery.sources.greenhouse import Greenhouse
from workers.operations import OperationCancelled


class GreenhouseCancellationCleanupTests(unittest.TestCase):
    def test_cancellation_does_not_wait_for_blocked_watcher_close(self):
        body_read_waiting = threading.Event()
        release_body = threading.Event()
        watcher_close_started = threading.Event()
        release_watcher_close = threading.Event()
        watcher_close_finished = threading.Event()
        watcher_threads = []

        class BlockingBody(httpx.SyncByteStream):
            def __iter__(self):
                yield b'{"jobs":'
                body_read_waiting.set()
                release_body.wait(2)

            def close(self):
                release_body.set()

        class BlockingTransport(httpx.BaseTransport):
            def handle_request(self, _request):
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=BlockingBody(),
                )

            def close(self):
                if threading.current_thread().name == "mjobs-greenhouse-cancellation":
                    watcher_close_started.set()
                    try:
                        release_watcher_close.wait(2)
                    finally:
                        watcher_close_finished.set()

        def check():
            if body_read_waiting.is_set():
                raise OperationCancelled("CANCEL_REQUESTED")

        check.remaining_seconds = lambda: 2.0
        adapter = Greenhouse(
            "example",
            transport=BlockingTransport(),
            check=check,
            request_timeout=2,
        )
        real_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            created = real_thread(*args, **kwargs)
            if kwargs.get("name") == "mjobs-greenhouse-cancellation":
                watcher_threads.append(created)
            return created

        threading_proxy = SimpleNamespace(
            Event=threading.Event,
            Lock=threading.Lock,
            Thread=capture_thread,
        )

        with patch.object(greenhouse_module, "threading", threading_proxy), patch.object(
                greenhouse_module, "CANCELLATION_POLL_INTERVAL", 0.01):
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(OperationCancelled, "CANCEL_REQUESTED"):
                    adapter._request("/jobs")
                elapsed = time.monotonic() - started
                self.assertTrue(watcher_close_started.wait(0.1))
                self.assertLess(elapsed, 0.35, "watcher cleanup delayed Greenhouse cancellation")
            finally:
                release_body.set()
                release_watcher_close.set()

        self.assertEqual(len(watcher_threads), 1)
        watcher_threads[0].join(0.5)
        self.assertFalse(watcher_threads[0].is_alive(), "Greenhouse cancellation watcher leaked")
        self.assertTrue(watcher_close_finished.is_set())

    def test_successful_search_keeps_board_and_jobs_requests(self):
        requests = []

        def handler(request):
            requests.append(request.url.path)
            payload = {"jobs": []} if request.url.path.endswith("/jobs") else {"name": "Example"}
            return httpx.Response(200, json=payload)

        batch = Greenhouse(
            "example",
            transport=httpx.MockTransport(handler),
            concurrency=2,
        ).search()

        self.assertEqual(
            set(requests),
            {"/v1/boards/example", "/v1/boards/example/jobs"},
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(batch.items, ())
        self.assertEqual(batch.source_company.name, "Example")


if __name__ == "__main__":
    unittest.main()
