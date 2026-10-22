"""Small response fragments must not delay Remotive deadline checks."""
import unittest
from unittest.mock import patch

import httpx

from services.discovery.sources.remotive import Remotive
from workers.operations import OperationTimedOut


class RemotiveBodyDeadlineTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
