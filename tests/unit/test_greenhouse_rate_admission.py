"""Greenhouse request admission stays paced after local setup and late wakes."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from services.discovery.sources import greenhouse


class AdmissionClock:
    """A module-local clock; HTTPX and watcher threads retain the real clock."""

    def __init__(self, now=0.0):
        self.now = now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class GreenhouseRateAdmissionTests(unittest.TestCase):
    def test_delayed_client_setup_does_not_consume_request_spacing(self):
        clock = AdmissionClock()
        dispatches = []
        clients = []
        real_client = httpx.Client

        def handler(request):
            dispatches.append(clock.now)
            return httpx.Response(200, json={"jobs": []})

        def delayed_client(*args, **kwargs):
            client = real_client(*args, **kwargs)
            clients.append(client)
            if len(clients) == 1:
                clock.now += 0.25
            return client

        adapter = greenhouse.Greenhouse("example", rate_limit=600)
        with patch.object(greenhouse, "time", clock), patch.object(
                greenhouse, "SourceTransport",
                side_effect=lambda check: httpx.MockTransport(handler)), patch.object(
                greenhouse.httpx, "Client", side_effect=delayed_client):
            adapter._request("/jobs")
            adapter._request("/jobs")

        self.assertEqual(len(dispatches), 2)
        self.assertAlmostEqual(dispatches[0], 0.25)
        self.assertAlmostEqual(dispatches[1] - dispatches[0], 0.1)
        self.assertTrue(all(client.is_closed for client in clients))

    def test_late_wakeup_rebases_the_next_request_admission(self):
        clock = AdmissionClock()
        sleeps = []

        def late_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:
                clock.now = 0.5
            else:
                clock.sleep(seconds)

        adapter = greenhouse.Greenhouse("example", rate_limit=600)
        adapter._last_request = 0.0
        clock_binding = SimpleNamespace(monotonic=clock.monotonic, sleep=late_sleep)
        with patch.object(greenhouse, "time", clock_binding):
            adapter._wait_for_rate_limit()
            first_admission = clock.now
            adapter._wait_for_rate_limit()
            second_admission = clock.now

        self.assertAlmostEqual(first_admission, 0.5)
        self.assertAlmostEqual(second_admission - first_admission, 0.1)
        self.assertTrue(sleeps)
        self.assertTrue(all(0 < delay <= 0.25 for delay in sleeps))

    def test_cancellation_after_pacing_sleep_closes_client_without_dispatch(self):
        clock = AdmissionClock()
        cancelled = [False]
        sleeps = []
        requests = []
        clients = []
        real_client = httpx.Client

        def check():
            if cancelled[0]:
                raise RuntimeError("CANCEL_REQUESTED")

        def sleep(seconds):
            sleeps.append(seconds)
            clock.sleep(seconds)
            cancelled[0] = True

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"jobs": []})

        def capture_client(*args, **kwargs):
            client = real_client(*args, **kwargs)
            clients.append(client)
            return client

        adapter = greenhouse.Greenhouse("example", rate_limit=600, check=check)
        adapter._last_request = 0.0
        clock_binding = SimpleNamespace(monotonic=clock.monotonic, sleep=sleep)
        with patch.object(greenhouse, "time", clock_binding), patch.object(
                greenhouse, "SourceTransport",
                side_effect=lambda check: httpx.MockTransport(handler)), patch.object(
                greenhouse.httpx, "Client", side_effect=capture_client), \
                self.assertRaisesRegex(RuntimeError, "CANCEL_REQUESTED"):
            adapter._request("/jobs")

        self.assertTrue(sleeps)
        self.assertEqual(requests, [])
        self.assertEqual(len(clients), 1)
        self.assertTrue(clients[0].is_closed)
        self.assertEqual(adapter.health(), "NOT_CHECKED")

    def test_dispatch_timeout_uses_budget_remaining_after_setup_and_admission(self):
        clock = AdmissionClock(now=1.0)
        worker_deadline = 1.2
        observed = []
        real_client = httpx.Client

        def check():
            return None

        check.remaining_seconds = lambda: worker_deadline - clock.now

        def handler(request):
            observed.append((clock.now, dict(request.extensions["timeout"])))
            return httpx.Response(200, json={"jobs": []})

        def delayed_client(*args, **kwargs):
            client = real_client(*args, **kwargs)
            clock.now += 0.02
            return client

        adapter = greenhouse.Greenhouse(
            "example", rate_limit=600, request_timeout=10, check=check)
        adapter._last_request = 0.95
        with patch.object(greenhouse, "time", clock), patch.object(
                greenhouse, "SourceTransport",
                side_effect=lambda check: httpx.MockTransport(handler)), patch.object(
                greenhouse.httpx, "Client", side_effect=delayed_client):
            adapter._request("/jobs")

        self.assertEqual(len(observed), 1)
        started, timeouts = observed[0]
        for kind in ("connect", "read", "write", "pool"):
            with self.subTest(timeout=kind):
                self.assertAlmostEqual(timeouts[kind], worker_deadline - started)


if __name__ == "__main__":
    unittest.main()
