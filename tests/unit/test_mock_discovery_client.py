"""Bounded, cancellable reads from the explicit local fixture origin."""
import threading
import unittest
from unittest.mock import patch

import httpx

from services.discovery.pipeline import MockClient


class MockDiscoveryClientTests(unittest.TestCase):
    def test_response_limit_stops_streaming_without_buffering_the_full_body(self):
        class OversizedBody(httpx.SyncByteStream):
            def __init__(self):
                self.chunks_yielded = 0
                self.closed = False

            def __iter__(self):
                for _ in range(100):
                    self.chunks_yielded += 1
                    yield b"x" * 65536

            def close(self):
                self.closed = True

        body = OversizedBody()
        response = httpx.Response(200, stream=body)
        real_client = httpx.Client
        transport = httpx.MockTransport(lambda _: response)

        def local_client(*args, **kwargs):
            return real_client(*args, transport=transport, **kwargs)

        with patch("services.discovery.pipeline.httpx.Client", side_effect=local_client):
            client = MockClient("http://127.0.0.1:8090")
        try:
            with self.assertRaisesRegex(ValueError, "Source response exceeds limit"):
                client.get("http://127.0.0.1:8090/jobs")
            self.assertTrue(body.closed)
            self.assertLess(body.chunks_yielded, 100)
        finally:
            client.close()

    def test_stalled_fixture_response_observes_worker_cancellation(self):
        cancelled = threading.Event()
        body_started = threading.Event()
        release_body = threading.Event()
        cancellation = RuntimeError("synthetic operation cancellation")
        errors = []

        class StalledBody(httpx.SyncByteStream):
            def __iter__(self):
                body_started.set()
                release_body.wait(timeout=2)
                yield b"{}"

            def close(self):
                release_body.set()

        def check():
            if cancelled.is_set():
                raise cancellation

        real_client = httpx.Client
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=StalledBody()))

        def local_client(*args, **kwargs):
            return real_client(*args, transport=transport, **kwargs)

        with patch("services.discovery.pipeline.httpx.Client", side_effect=local_client):
            client = MockClient("http://127.0.0.1:8090", check=check)

        def request():
            try:
                client.get("http://127.0.0.1:8090/jobs")
            except Exception as error:
                errors.append(error)

        try:
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(body_started.wait(timeout=1), "fixture response read did not start")
            cancelled.set()
            thread.join(timeout=0.3)
            self.assertFalse(thread.is_alive(), "fixture response ignored worker cancellation")
            self.assertEqual(errors, [cancellation])
        finally:
            release_body.set()
            client.close()


if __name__ == "__main__":
    unittest.main()
