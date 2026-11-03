"""Actual read-only adapters must retain provider-requested cooldowns."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import math
import unittest

import httpx

from services.discovery.sources.errors import SourceError
from services.discovery.sources.greenhouse import Greenhouse
from services.discovery.sources.remotive import Remotive
from services.discovery.sources.remoteok import RemoteOK
from services.discovery.sources.wwr import WeWorkRemotely


ADAPTERS = (Remotive, RemoteOK, WeWorkRemotely, Greenhouse)


class SourceRetryAfterTests(unittest.TestCase):
    def failure(self, adapter_type, status, header):
        requests = []

        def respond(request):
            requests.append(request)
            headers = {} if header is None else {"Retry-After": header}
            return httpx.Response(status, headers=headers)

        adapter = adapter_type("synthetic", transport=httpx.MockTransport(respond))
        with self.assertRaises(SourceError) as failure:
            adapter.search()
        expected_requests = 2 if adapter_type is Greenhouse else 1
        self.assertEqual(len(requests), expected_requests)
        self.assertEqual(len({str(request.url) for request in requests}), expected_requests,
                         "Existing independent endpoints may each be fetched once; no replay")
        self.assertTrue(all(request.method == "GET" for request in requests))
        expected = ("SOURCE_RATE_LIMITED" if status == 429 else
                    f"SOURCE_HTTP_{status}" if adapter_type is Greenhouse else "SOURCE_SERVER_ERROR")
        self.assertEqual(failure.exception.code, expected)
        self.assertEqual(adapter.health(), expected)
        self.assertNotIn("Retry-After", str(failure.exception))
        return failure.exception.retry_seconds

    def test_http_dates_on_429_and_503_preserve_the_requested_interval(self):
        for adapter in ADAPTERS:
            for status in (429, 503):
                with self.subTest(adapter=adapter.__name__, status=status):
                    requested = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
                    before = datetime.now(timezone.utc)
                    delay = self.failure(adapter, status, format_datetime(requested, usegmt=True))
                    after = datetime.now(timezone.utc)
                    # Relational bounds cover the receipt clock without sleeps
                    # or assumptions about how quickly a loaded host executes.
                    self.assertGreaterEqual(delay, math.ceil((requested - after).total_seconds()))
                    self.assertLessEqual(delay, math.ceil((requested - before).total_seconds()))

    def test_greenhouse_retains_longest_cooldown_from_its_independent_requests(self):
        for concurrency in (1, 2):
            for status in (429, 503):
                with self.subTest(concurrency=concurrency, status=status):
                    calls = []
                    def respond(request):
                        calls.append(str(request.url))
                        delay = "7200" if request.url.path.endswith("/jobs") else "60"
                        return httpx.Response(status, headers={"Retry-After": delay})
                    adapter = Greenhouse("synthetic", transport=httpx.MockTransport(respond),
                                         concurrency=concurrency)
                    with self.assertRaises(SourceError) as failure:
                        adapter.search()
                    self.assertEqual(failure.exception.retry_seconds, 7200)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(len(set(calls)), 2)

    def test_503_numeric_cooldown_is_not_discarded(self):
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter.__name__):
                self.assertEqual(self.failure(adapter, 503, "7200"), 7200)

    def test_existing_missing_invalid_and_short_numeric_policies_are_preserved(self):
        for adapter in ADAPTERS:
            for header in (None, "not-an-http-date", "45"):
                with self.subTest(adapter=adapter.__name__, header=header):
                    expected = (86400 if header == "not-an-http-date" else 60) if adapter is Greenhouse else (
                        45 if header == "45" else 60)
                    self.assertEqual(self.failure(adapter, 429, header), expected)
            with self.subTest(adapter=adapter.__name__, status=503):
                fallback = 60 if adapter is Greenhouse else 300
                self.assertEqual(self.failure(adapter, 503, None), fallback)
                self.assertEqual(self.failure(adapter, 503, "invalid"), fallback)

    def test_greenhouse_keeps_mixed_network_and_provider_cooldowns(self):
        for concurrency in (1, 2):
            for network_on_jobs in (False, True):
                for status in (429, 503):
                    with self.subTest(concurrency=concurrency, network_on_jobs=network_on_jobs,
                                      status=status):
                        calls = []
                        def respond(request):
                            calls.append(str(request.url))
                            if request.url.path.endswith("/jobs") == network_on_jobs:
                                raise httpx.ReadTimeout("synthetic-private-provider-detail", request=request)
                            return httpx.Response(status, headers={"Retry-After": "7200"})
                        adapter = Greenhouse("synthetic", transport=httpx.MockTransport(respond),
                                             concurrency=concurrency)
                        with self.assertRaises(SourceError) as failure:
                            adapter.search()
                        expected = ("SOURCE_RATE_LIMITED" if status == 429 else "SOURCE_HTTP_503"
                                    ) if network_on_jobs else "SOURCE_NETWORK_ERROR"
                        self.assertEqual(failure.exception.code, expected)
                        self.assertEqual(failure.exception.retry_seconds, 7200)
                        self.assertNotIn("synthetic-private", str(failure.exception))
                        self.assertEqual(len(calls), 2)
                        self.assertEqual(len(set(calls)), 2)
