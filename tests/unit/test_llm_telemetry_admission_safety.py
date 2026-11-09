"""Adversarial admission observers cannot leak capacity or extend deadlines."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from services.ai.providers import AIProviderError, AzureOpenAIProvider, OpenAICompatibleProvider
from services.ai.telemetry import AIConcurrencyLimitError, LLMTelemetryEngine


class AdmissionObserverSafetyTests(unittest.TestCase):
    def test_observer_failure_before_admission_is_sanitized(self):
        engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)

        class BrokenObserver:
            def is_set(self):
                raise RuntimeError("private observer failure sentinel")

        with self.assertRaises(RuntimeError) as error:
            with engine.acquire_slot("first", cancellation_event=BrokenObserver()):
                self.fail("A failed observer must never admit work")

        self.assertEqual(str(error.exception), "AI_ADMISSION_OBSERVER_UNAVAILABLE")
        self.assertEqual(engine.get_summary()["active_concurrency"], 0)
        with engine.acquire_slot("recovery", timeout_seconds=0.1):
            self.assertEqual(engine.get_summary()["active_concurrency"], 1)

    def test_observer_failure_after_reservation_releases_capacity(self):
        engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
        entered = []

        class BrokenAfterReservation:
            def is_set(self):
                # Fault injection targets the reserved state, not a particular
                # number of cancellation polls in the implementation.
                if engine._active_global:
                    raise RuntimeError("private reserved observer sentinel")
                return False

        with self.assertRaises(RuntimeError) as error:
            with engine.acquire_slot("first", cancellation_event=BrokenAfterReservation()):
                entered.append(True)

        self.assertEqual(entered, [])
        self.assertEqual(engine.get_summary()["active_concurrency"], 0)
        self.assertEqual(engine._active_by_provider.get("first", 0), 0)
        self.assertEqual(str(error.exception), "AI_ADMISSION_OBSERVER_UNAVAILABLE")
        with engine.acquire_slot("recovery", timeout_seconds=0.1):
            self.assertEqual(engine.get_summary()["active_concurrency"], 1)

    def test_observer_failure_preserves_another_requests_reservation(self):
        for after_reservation in (False, True):
            with self.subTest(after_reservation=after_reservation):
                engine = LLMTelemetryEngine(global_concurrency_limit=2, budget_ceiling_usd=None)

                class BrokenObserver:
                    def is_set(self):
                        if not after_reservation or engine._active_global > 1:
                            raise RuntimeError("private candidate observer sentinel")
                        return False

                with engine.acquire_slot("existing"):
                    with self.assertRaises(RuntimeError) as error:
                        with engine.acquire_slot("candidate", cancellation_event=BrokenObserver()):
                            self.fail("A failed observer must never admit work")
                    self.assertEqual(str(error.exception), "AI_ADMISSION_OBSERVER_UNAVAILABLE")
                    self.assertEqual(engine.get_summary()["active_concurrency"], 1)
                    self.assertEqual(engine._active_by_provider["existing"], 1)
                    self.assertEqual(engine._active_by_provider.get("candidate", 0), 0)
                    with engine.acquire_slot("recovery", timeout_seconds=0.1):
                        self.assertEqual(engine.get_summary()["active_concurrency"], 2)
                self.assertEqual(engine.get_summary()["active_concurrency"], 0)

    def test_blocked_observer_does_not_block_unrelated_admission(self):
        engine = LLMTelemetryEngine(global_concurrency_limit=2, budget_ceiling_usd=None)
        observing = threading.Event()
        release_observer = threading.Event()
        unrelated_finished = threading.Event()
        errors = []

        class BlockingObserver:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                if self.calls == 2:
                    observing.set()
                    if not release_observer.wait(5):
                        raise RuntimeError("test observer was not released")
                return False

        def observed_request():
            try:
                with engine.acquire_slot(
                    "observed", timeout_seconds=10, cancellation_event=BlockingObserver()
                ):
                    pass
            except Exception as error:
                errors.append(error)

        def unrelated_request():
            try:
                with engine.acquire_slot("unrelated", timeout_seconds=10):
                    pass
                unrelated_finished.set()
            except Exception as error:
                errors.append(error)

        observed = threading.Thread(target=observed_request, daemon=True)
        unrelated = threading.Thread(target=unrelated_request, daemon=True)
        observed.start()
        try:
            self.assertTrue(observing.wait(2), "observer checkpoint was not reached")
            unrelated.start()
            # This is an ordering assertion: the second request must finish
            # while the observer remains held, not a provider latency target.
            self.assertTrue(
                unrelated_finished.wait(2),
                "An observer held the telemetry lock against unrelated work",
            )
        finally:
            release_observer.set()
            observed.join(2)
            if unrelated.ident is not None:
                unrelated.join(2)
        self.assertFalse(observed.is_alive())
        self.assertFalse(unrelated.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(engine.get_summary()["active_concurrency"], 0)

    def test_deadline_crossed_before_reservation_never_admits_work(self):
        engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
        ticks = iter((0.0, 0.0))
        entered = []

        def clock():
            # Deadline creation and the initial loop check occur before the
            # deadline. Every later observation models time spent waiting for
            # admission's shared lock, after the deadline has elapsed.
            return next(ticks, 2.0)

        with patch("services.ai.telemetry.time.monotonic", side_effect=clock):
            with self.assertRaises(AIConcurrencyLimitError):
                with engine.acquire_slot("first", timeout_seconds=1):
                    entered.append(True)
        self.assertEqual(entered, [])
        self.assertEqual(engine.get_summary()["active_concurrency"], 0)

    def test_observer_elapsed_time_cannot_extend_admission_deadline(self):
        for after_reservation in (False, True):
            with self.subTest(after_reservation=after_reservation):
                engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
                now = [0.0]
                entered = []

                class SlowObserver:
                    def __init__(self):
                        self.calls = 0

                    def is_set(self):
                        self.calls += 1
                        if (after_reservation and engine._active_global) or (
                            not after_reservation and self.calls == 2
                        ):
                            now[0] = 2.0
                        return False

                with patch("services.ai.telemetry.time.monotonic", side_effect=lambda: now[0]):
                    with self.assertRaises(AIConcurrencyLimitError):
                        with engine.acquire_slot(
                            "first", timeout_seconds=1, cancellation_event=SlowObserver()
                        ):
                            entered.append(True)
                self.assertEqual(entered, [])
                self.assertEqual(engine.get_summary()["active_concurrency"], 0)
                self.assertEqual(engine._active_by_provider.get("first", 0), 0)

    def test_provider_observer_failure_is_unsent_sanitized_and_recoverable(self):
        cases = (
            (OpenAICompatibleProvider, "services.ai.providers.openai_compatible"),
            (AzureOpenAIProvider, "services.ai.providers.azure_openai"),
        )
        for provider_type, module in cases:
            for after_reservation in (False, True):
                with self.subTest(provider=provider_type.name, after_reservation=after_reservation):
                    engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
                    factories = []
                    errors = []

                    class BrokenObserver:
                        def is_set(self):
                            if not after_reservation or engine._active_global:
                                raise RuntimeError("private provider observer sentinel")
                            return False

                    def forbidden_factory(**kwargs):
                        factories.append(kwargs)
                        raise AssertionError("Transport must not be constructed")

                    provider = provider_type(client_factory=forbidden_factory)
                    with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                        try:
                            provider.generate(
                                task="scoring", model="gpt-4o", prompt="synthetic test prompt",
                                timeout_seconds=1, cancellation_event=BrokenObserver(),
                            )
                        except Exception as error:
                            errors.append(error)

                    self.assertEqual(factories, [])
                    summary = engine.get_summary()
                    self.assertEqual(summary["active_concurrency"], 0)
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], AIProviderError)
                    self.assertEqual(str(errors[0]), "AI_PROVIDER_UNAVAILABLE")
                    self.assertEqual(summary["total_calls"], 1)
                    attempt = summary["recent_attempts"][0]
                    self.assertFalse(attempt["request_sent"])
                    self.assertEqual(attempt["usage_provenance"], "NOT_SENT")
                    self.assertEqual(attempt["error_code"], "AI_PROVIDER_UNAVAILABLE")
                    self.assertEqual(attempt["estimated_cost"], 0.0)
                    self.assertNotIn("private provider observer sentinel", str(summary))
                    with engine.acquire_slot("recovery", timeout_seconds=0.1):
                        self.assertEqual(engine.get_summary()["active_concurrency"], 1)


if __name__ == "__main__":
    unittest.main()
