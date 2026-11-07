"""Production provider admission and usage provenance for MJ-REF-015."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

import httpx

from services.ai.providers import AIProviderError, AzureOpenAIProvider, OpenAICompatibleProvider
from services.ai.telemetry import LLMTelemetryEngine


class ProviderTelemetryAdmissionTests(unittest.TestCase):
    def _cases(self):
        return (
            (OpenAICompatibleProvider, "services.ai.providers.openai_compatible"),
            (AzureOpenAIProvider, "services.ai.providers.azure_openai"),
        )

    def test_provider_cap_blocks_transport_and_records_rejection(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                requests = []
                engine = LLMTelemetryEngine(
                    global_concurrency_limit=2,
                    provider_concurrency_limits={provider_type.name: 1},
                    budget_ceiling_usd=None,
                )
                provider = provider_type(
                    client_factory=lambda **kw: httpx.Client(
                        transport=httpx.MockTransport(
                            lambda req: requests.append(req) or httpx.Response(200, json={})
                        ), **kw
                    )
                )
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with engine.acquire_slot(provider.name):
                        with self.assertRaises(AIProviderError) as error:
                            provider.generate(
                                task="scoring", model="gpt-4o", prompt="test",
                                timeout_seconds=0.03,
                            )
                self.assertEqual(error.exception.code, "AI_PROVIDER_CONCURRENCY_LIMIT")
                self.assertEqual(requests, [])
                attempt = engine.get_summary()["recent_attempts"][-1]
                self.assertEqual(attempt["status"], "RATE_LIMITED")
                self.assertEqual(attempt["error_code"], "AI_PROVIDER_CONCURRENCY_LIMIT")
                self.assertFalse(attempt["request_sent"])
                self.assertEqual(attempt["usage_provenance"], "NOT_SENT")
                self.assertEqual(attempt["estimated_cost"], 0.0)
                self.assertEqual(engine.get_summary()["unknown_cost_calls"], 0)

    def test_budget_stop_blocks_transport(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                requests = []
                engine = LLMTelemetryEngine(budget_ceiling_usd=0)
                provider = provider_type(
                    client_factory=lambda **kw: httpx.Client(
                        transport=httpx.MockTransport(
                            lambda req: requests.append(req) or httpx.Response(200, json={})
                        ), **kw
                    )
                )
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with self.assertRaises(AIProviderError) as error:
                        provider.generate(task="scoring", model="gpt-4o", prompt="test", timeout_seconds=1)
                self.assertEqual(error.exception.code, "AI_PROVIDER_BUDGET_EXCEEDED")
                self.assertEqual(requests, [])
                self.assertEqual(engine.get_summary()["recent_attempts"][-1]["status"], "BUDGET_STOP")

    def test_missing_usage_is_unknown_and_failure_is_recorded(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                engine = LLMTelemetryEngine(budget_ceiling_usd=None)
                responses = [
                    httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
                    httpx.Response(503, text="unavailable"),
                ]
                provider = provider_type(
                    client_factory=lambda **kw: httpx.Client(
                        transport=httpx.MockTransport(lambda req: responses.pop(0)), **kw
                    )
                )
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    self.assertEqual(
                        provider.generate(task="scoring", model="gpt-4o", prompt="test", timeout_seconds=1),
                        "ok",
                    )
                    with self.assertRaises(AIProviderError):
                        provider.generate(task="scoring", model="gpt-4o", prompt="test", timeout_seconds=1)
                summary = engine.get_summary()
                self.assertEqual(summary["successful_calls"], 1)
                self.assertEqual(summary["failed_calls"], 1)
                self.assertEqual(summary["unknown_usage_calls"], 2)
                self.assertEqual(summary["unknown_cost_calls"], 2)
                self.assertIsNone(summary["recent_attempts"][0]["prompt_tokens"])
                self.assertEqual(summary["recent_attempts"][0]["cost_provenance"], "UNAVAILABLE")
                self.assertEqual(summary["recent_attempts"][1]["error_code"], "AI_PROVIDER_HTTP_ERROR")


    def test_cancel_while_queued_does_not_send_or_leak_slot(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                requests = []
                cancel = threading.Event()
                engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
                provider = provider_type(client_factory=lambda **kw: httpx.Client(
                    transport=httpx.MockTransport(
                        lambda req: requests.append(req) or httpx.Response(200, json={})
                    ), **kw
                ))
                errors = []

                def queued_call():
                    try:
                        provider.generate(
                            task="scoring", model="gpt-4o", prompt="test",
                            timeout_seconds=1, cancellation_event=cancel,
                        )
                    except AIProviderError as error:
                        errors.append(error.code)

                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with engine.acquire_slot(provider.name):
                        worker = threading.Thread(target=queued_call)
                        worker.start()
                        time.sleep(0.03)
                        cancel.set()
                        worker.join(timeout=0.5)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(engine.get_summary()["active_concurrency"], 1)
                self.assertEqual(errors, ["AI_PROVIDER_CANCELLED"])
                self.assertEqual(requests, [])
                summary = engine.get_summary()
                self.assertEqual(summary["active_concurrency"], 0)
                self.assertFalse(summary["recent_attempts"][-1]["request_sent"])
                self.assertEqual(summary["unknown_usage_calls"], 0)

    def test_queued_call_observes_budget_recorded_before_slot_release(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                entered = threading.Event()
                release = threading.Event()
                requests = []
                errors = []
                engine = LLMTelemetryEngine(
                    global_concurrency_limit=1, budget_ceiling_usd=2.5
                )

                def handler(request):
                    requests.append(request)
                    entered.set()
                    release.wait(1)
                    return httpx.Response(200, json={
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
                    })

                provider = provider_type(client_factory=lambda **kw: httpx.Client(
                    transport=httpx.MockTransport(handler), **kw
                ))

                def first_call():
                    try:
                        provider.generate(
                            task="scoring", model="gpt-4o", prompt="test",
                            timeout_seconds=2,
                        )
                    except AIProviderError as error:
                        errors.append(error.code)

                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    worker = threading.Thread(target=first_call)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        second = threading.Thread(target=lambda: self._capture_second(provider, errors))
                        second.start()
                        time.sleep(0.03)
                    finally:
                        release.set()
                        worker.join(timeout=2)
                        second.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, ["AI_PROVIDER_BUDGET_EXCEEDED"])
                self.assertEqual(len(requests), 1)
                summary = engine.get_summary()
                self.assertEqual(summary["total_cost_usd"], 2.5)
                self.assertEqual(summary["active_concurrency"], 0)
                self.assertEqual(summary["unknown_cost_calls"], 0)

    @staticmethod
    def _capture_second(provider, errors):
        try:
            provider.generate(task="scoring", model="gpt-4o", prompt="test", timeout_seconds=2)
        except AIProviderError as error:
            errors.append(error.code)


if __name__ == "__main__":
    unittest.main()
