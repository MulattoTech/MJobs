"""Greenhouse request admission stays paced after local setup and late wakes."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from services.discovery.sources import greenhouse
from workers.operations import OperationCancelled, OperationControlUnavailable, OperationTimedOut


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

    def test_slow_budget_refresh_does_not_consume_request_spacing(self):
        clock = AdmissionClock()
        dispatches = []
        client_ready = [False]
        delayed = [False]
        real_client = httpx.Client

        def check():
            return None

        def remaining_seconds():
            if client_ready[0] and not delayed[0] and not dispatches:
                clock.now += 0.25
                delayed[0] = True
            return 10.0 - clock.now

        check.remaining_seconds = remaining_seconds

        def handler(request):
            dispatches.append(clock.now)
            return httpx.Response(200, json={"jobs": []})

        def capture_client(*args, **kwargs):
            client = real_client(*args, **kwargs)
            client_ready[0] = True
            return client

        adapter = greenhouse.Greenhouse("example", rate_limit=600, check=check)
        with patch.object(greenhouse, "time", clock), patch.object(
                greenhouse, "SourceTransport",
                side_effect=lambda check: httpx.MockTransport(handler)), patch.object(
                greenhouse.httpx, "Client", side_effect=capture_client):
            adapter._request("/jobs")
            adapter._request("/jobs")

        self.assertTrue(delayed[0], "the fixture must delay a pre-dispatch budget refresh")
        self.assertEqual(len(dispatches), 2)
        self.assertAlmostEqual(dispatches[1] - dispatches[0], 0.1)

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

    def test_exhausted_budget_preserves_local_stop_without_dispatch(self):
        for failure in (None, OperationCancelled("cancelled"), OperationTimedOut("expired")):
            with self.subTest(stop=type(failure).__name__):
                clock = AdmissionClock()
                requests = []
                clients = []
                real_client = httpx.Client

                def check():
                    if failure is not None and clock.now > 0 and adapter._last_request == clock.now:
                        raise failure

                check.remaining_seconds = lambda: 0.04

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json={"jobs": []})

                def capture_client(*args, **kwargs):
                    client = real_client(*args, **kwargs)
                    clients.append(client)
                    return client

                adapter = greenhouse.Greenhouse("example", rate_limit=600, check=check)
                adapter._last_request = 0.0
                expected = type(failure) if failure is not None else OperationTimedOut
                with patch.object(greenhouse, "time", clock), patch.object(
                        greenhouse, "SourceTransport",
                        side_effect=lambda check: httpx.MockTransport(handler)), patch.object(
                        greenhouse.httpx, "Client", side_effect=capture_client), \
                        self.assertRaises(expected) as raised:
                    adapter._request("/jobs")

                if failure is not None:
                    self.assertIs(raised.exception, failure)
                else:
                    self.assertEqual(str(raised.exception), "OPERATION_TIMEOUT")
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


class GreenhouseTimeoutOriginTests(unittest.TestCase):
    def run_timeout(self, *, budget, timeout_type=httpx.ConnectTimeout,
                    local_stop=None, expire=True, late_check_delay=0):
        """HTTPX raises before a deliberately unscheduled cancellation watcher."""
        clock = AdmissionClock()
        completed = [False]
        original_errors = []
        clients = []
        real_client = httpx.Client

        def check():
            if completed[0]:
                clock.now += late_check_delay
                if local_stop is not None:
                    raise local_stop

        if budget is not None:
            check.remaining_seconds = lambda: budget - clock.now

        def handler(request):
            if expire:
                clock.now += request.extensions["timeout"]["connect"]
            completed[0] = True
            error = timeout_type("Synthetic selected timeout", request=request)
            original_errors.append(error)
            raise error

        def capture_client(*args, **kwargs):
            client = real_client(*args, **kwargs)
            clients.append(client)
            return client

        class PendingWatcher:
            def __init__(self, **kwargs):
                pass

            def start(self):
                pass

            def is_alive(self):
                return False

        # Preserve real Event/Lock primitives; only watcher scheduling is held.
        threading_binding = SimpleNamespace(
            Event=greenhouse.threading.Event, Thread=PendingWatcher)
        adapter = greenhouse.Greenhouse("example", check=check, request_timeout=2)
        outcome = None
        with patch.object(greenhouse, "time", clock), patch.object(
                greenhouse, "threading", threading_binding), patch.object(
                greenhouse, "SourceTransport",
                side_effect=lambda check: httpx.MockTransport(handler)), patch.object(
                greenhouse.httpx, "Client", side_effect=capture_client):
            try:
                adapter._get("/jobs")
            except Exception as error:
                outcome = error

        self.assertEqual(len(original_errors), 1)
        self.assertEqual(len(clients), 1)
        self.assertTrue(clients[0].is_closed)
        self.assertIsNotNone(outcome)
        return outcome, original_errors[0], adapter.health()

    def test_worker_budget_timeout_preserves_unchecked_source_without_watcher(self):
        for timeout_type in (httpx.ConnectTimeout, httpx.ReadTimeout,
                             httpx.WriteTimeout, httpx.PoolTimeout):
            with self.subTest(timeout=timeout_type.__name__):
                outcome, original, health = self.run_timeout(
                    budget=0.04, timeout_type=timeout_type)
                self.assertIsInstance(outcome, OperationTimedOut)
                self.assertEqual(str(outcome), "OPERATION_TIMEOUT")
                self.assertIs(outcome.__cause__, original)
                self.assertEqual(health, "NOT_CHECKED")

    def test_shorter_provider_timeout_retains_provider_failure(self):
        for budget in (None, 10.0):
            for timeout_type in (httpx.ConnectTimeout, httpx.ReadTimeout,
                                 httpx.WriteTimeout, httpx.PoolTimeout):
                with self.subTest(budget=budget, timeout=timeout_type.__name__):
                    outcome, original, health = self.run_timeout(
                        budget=budget, timeout_type=timeout_type)
                    self.assertIs(outcome, original)
                    self.assertEqual(health, "SOURCE_NETWORK_ERROR")

    def test_provider_failure_is_not_replaced_by_a_later_slow_worker_check(self):
        outcome, original, health = self.run_timeout(
            budget=10.0, local_stop=OperationTimedOut("OPERATION_TIMEOUT"),
            late_check_delay=9.0)
        self.assertIs(outcome, original)
        self.assertEqual(health, "SOURCE_NETWORK_ERROR")

    def test_budget_timeout_preserves_authoritative_local_stop(self):
        for stop in (OperationCancelled("CANCEL_REQUESTED"),
                     OperationControlUnavailable(),
                     OperationTimedOut("OPERATION_TIMEOUT")):
            with self.subTest(stop=type(stop).__name__):
                outcome, _original, health = self.run_timeout(budget=0.04, local_stop=stop)
                self.assertIs(outcome, stop)
                self.assertEqual(health, "NOT_CHECKED")

    def test_early_transport_timeout_does_not_claim_worker_budget_expired(self):
        outcome, original, health = self.run_timeout(budget=0.04, expire=False)
        self.assertIs(outcome, original)
        self.assertEqual(health, "SOURCE_NETWORK_ERROR")

    def test_budget_callback_latency_is_not_subtracted_twice(self):
        clock = AdmissionClock()
        worker_deadline = 1.0
        observed = []

        def check():
            return None

        def remaining_seconds():
            # The worker returns remaining time after its durable control read.
            clock.now += 0.1
            return worker_deadline - clock.now

        check.remaining_seconds = remaining_seconds

        def handler(request):
            observed.append((clock.now, dict(request.extensions["timeout"])))
            return httpx.Response(200, json={"jobs": []})

        adapter = greenhouse.Greenhouse("example", check=check, request_timeout=2)
        with patch.object(greenhouse, "time", clock), patch.object(
                greenhouse, "SourceTransport",
                side_effect=lambda check: httpx.MockTransport(handler)):
            adapter._get("/jobs")

        self.assertEqual(len(observed), 1)
        dispatched_at, timeouts = observed[0]
        for kind in ("connect", "read", "write", "pool"):
            with self.subTest(timeout=kind):
                self.assertAlmostEqual(timeouts[kind], worker_deadline - dispatched_at)


if __name__ == "__main__":
    unittest.main()
