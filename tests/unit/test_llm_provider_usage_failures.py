"""Preserve provider-attempt truth and settle failed-response usage before release."""
from __future__ import annotations

from contextlib import contextmanager
import json
import threading
import unittest
from unittest.mock import patch

import httpx

from services.ai.providers import AIProviderError, AzureOpenAIProvider, OpenAICompatibleProvider
from services.ai.telemetry import LLMTelemetryEngine


class ProviderFailureUsageTests(unittest.TestCase):
    def _cases(self):
        return (
            (OpenAICompatibleProvider, "services.ai.providers.openai_compatible"),
            (AzureOpenAIProvider, "services.ai.providers.azure_openai"),
        )

    def _generate(self, provider, **kwargs):
        return provider.generate(
            task="scoring", model="gpt-4o", prompt="private prompt sentinel",
            timeout_seconds=2, **kwargs,
        )

    def test_factory_failure_is_not_sent_and_sanitized(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                engine = LLMTelemetryEngine(budget_ceiling_usd=None)

                def factory(**_kwargs):
                    raise RuntimeError("private factory sentinel")

                provider = provider_type(client_factory=factory)
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with self.assertRaises(AIProviderError) as error:
                        self._generate(provider)
                self.assertEqual(error.exception.code, "AI_PROVIDER_UNAVAILABLE")
                summary = engine.get_summary()
                self.assertEqual(summary["active_concurrency"], 0)
                self.assertEqual(summary["total_calls"], 1)
                attempt = summary["recent_attempts"][0]
                self.assertFalse(attempt["request_sent"])
                self.assertEqual(attempt["usage_provenance"], "NOT_SENT")
                self.assertEqual(attempt["estimated_cost"], 0.0)
                self.assertEqual(summary["unknown_cost_calls"], 0)
                self.assertNotIn("sentinel", json.dumps(summary))

    def test_guard_setup_failure_is_not_sent_and_closes_client(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                requests = []
                clients = []
                engine = LLMTelemetryEngine(budget_ceiling_usd=None)

                def factory(**kwargs):
                    client = httpx.Client(transport=httpx.MockTransport(
                        lambda request: requests.append(request) or httpx.Response(200, json={})
                    ), **kwargs)
                    clients.append(client)
                    return client

                provider = provider_type(client_factory=factory)
                with (
                    patch(f"{module}.get_default_telemetry_engine", return_value=engine),
                    patch(f"{module}.request_guard", side_effect=AIProviderError("AI_PROVIDER_TIMEOUT")),
                ):
                    with self.assertRaises(AIProviderError) as error:
                        self._generate(provider)
                self.assertEqual(error.exception.code, "AI_PROVIDER_TIMEOUT")
                self.assertEqual(requests, [])
                self.assertEqual(len(clients), 1)
                self.assertTrue(clients[0].is_closed)
                summary = engine.get_summary()
                self.assertEqual(summary["total_calls"], 1)
                self.assertFalse(summary["recent_attempts"][0]["request_sent"])
                self.assertEqual(summary["active_concurrency"], 0)

    def test_initial_observer_failure_is_not_sent_and_sanitized(self):
        class BrokenObserver:
            def is_set(self):
                raise RuntimeError("private observer sentinel")

        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                engine = LLMTelemetryEngine(budget_ceiling_usd=None)
                factory_calls = []
                provider = provider_type(client_factory=lambda **_kwargs: factory_calls.append(True))
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with self.assertRaises(AIProviderError) as error:
                        self._generate(provider, cancellation_event=BrokenObserver())
                self.assertEqual(error.exception.code, "AI_PROVIDER_UNAVAILABLE")
                self.assertEqual(factory_calls, [])
                summary = engine.get_summary()
                self.assertEqual(summary["total_calls"], 1)
                self.assertFalse(summary["recent_attempts"][0]["request_sent"])
                self.assertEqual(summary["active_concurrency"], 0)
                self.assertNotIn("sentinel", json.dumps(summary))

    def test_initial_observer_cannot_restart_the_provider_deadline(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
                ticks = [0.0]
                factories = []
                requests = []

                class SlowObserver:
                    calls = 0

                    def is_set(self):
                        self.calls += 1
                        if self.calls == 1:
                            ticks[0] = 2.0
                        return False

                def factory(**kwargs):
                    factories.append(True)
                    return httpx.Client(transport=httpx.MockTransport(
                        lambda request: requests.append(request) or httpx.Response(200, json={})
                    ), **kwargs)

                provider = provider_type(client_factory=factory)
                with (
                    patch(f"{module}.get_default_telemetry_engine", return_value=engine),
                    patch(f"{module}.time.monotonic", side_effect=lambda: ticks[0]),
                ):
                    with self.assertRaises(AIProviderError) as error:
                        provider.generate(task="scoring", model="gpt-4o", prompt="synthetic",
                                          timeout_seconds=1, cancellation_event=SlowObserver())
                self.assertEqual(error.exception.code, "AI_PROVIDER_TIMEOUT")
                self.assertEqual(factories, [])
                self.assertEqual(requests, [])
                summary = engine.get_summary()
                self.assertEqual(summary["active_concurrency"], 0)
                self.assertEqual(summary["total_calls"], 1)
                self.assertFalse(summary["recent_attempts"][0]["request_sent"])

    def test_guard_preserves_observer_failure_before_later_cancellation(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                engine = LLMTelemetryEngine(global_concurrency_limit=1, budget_ceiling_usd=None)
                caller = threading.get_ident()
                phase = {"responded": False, "caller_polls": 0}

                class ChangingObserver:
                    def is_set(self):
                        if not phase["responded"] or threading.get_ident() != caller:
                            return False
                        phase["caller_polls"] += 1
                        if phase["caller_polls"] == 1:
                            return False  # Initial response ownership check.
                        if phase["caller_polls"] == 2:
                            raise RuntimeError("private observer sentinel")
                        return True

                def handler(_request):
                    phase["responded"] = True
                    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

                provider = provider_type(client_factory=lambda **kwargs: httpx.Client(
                    transport=httpx.MockTransport(handler), **kwargs,
                ))
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with self.assertRaises(AIProviderError) as error:
                        self._generate(provider, cancellation_event=ChangingObserver())
                self.assertEqual(error.exception.code, "AI_PROVIDER_UNAVAILABLE")
                self.assertEqual(phase["caller_polls"], 2)
                summary = engine.get_summary()
                self.assertEqual(summary["active_concurrency"], 0)
                self.assertEqual(summary["total_calls"], 1)
                attempt = summary["recent_attempts"][0]
                self.assertEqual(attempt["error_code"], "AI_PROVIDER_UNAVAILABLE")
                self.assertTrue(attempt["request_sent"])
                self.assertNotIn("sentinel", json.dumps(summary))

    def test_transport_failure_is_sent_with_unknown_usage(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                engine = LLMTelemetryEngine(budget_ceiling_usd=None)
                requests = []

                def handler(request):
                    requests.append(request)
                    raise httpx.ConnectError("private transport sentinel", request=request)

                provider = provider_type(client_factory=lambda **kwargs: httpx.Client(
                    transport=httpx.MockTransport(handler), **kwargs,
                ))
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with self.assertRaises(AIProviderError) as error:
                        self._generate(provider)
                self.assertEqual(error.exception.code, "AI_PROVIDER_UNAVAILABLE")
                self.assertEqual(len(requests), 1)
                summary = engine.get_summary()
                self.assertEqual(summary["total_calls"], 1)
                attempt = summary["recent_attempts"][0]
                self.assertTrue(attempt["request_sent"])
                self.assertEqual(attempt["usage_provenance"], "UNAVAILABLE")
                self.assertIsNone(attempt["estimated_cost"])
                self.assertNotIn("sentinel", json.dumps(summary))

    def test_telemetry_sink_failure_does_not_retry_or_replace_result(self):
        for provider_type, module in self._cases():
            for content in ("ok", ""):
                with self.subTest(provider=provider_type.__name__, content=content):
                    engine = LLMTelemetryEngine(budget_ceiling_usd=None)
                    sink_calls = []
                    original_record = engine.record_attempt

                    def failing_sink(**kwargs):
                        sink_calls.append(True)
                        original_record(**kwargs)
                        raise RuntimeError("private telemetry sentinel")

                    provider = provider_type(client_factory=lambda **kwargs: httpx.Client(
                        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
                            "choices": [{"message": {"content": content}}],
                            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
                        })), **kwargs,
                    ))
                    with (
                        patch(f"{module}.get_default_telemetry_engine", return_value=engine),
                        patch.object(engine, "record_attempt", side_effect=failing_sink),
                    ):
                        if content:
                            self.assertEqual(self._generate(provider), "ok")
                        else:
                            with self.assertRaises(AIProviderError) as error:
                                self._generate(provider)
                            self.assertEqual(error.exception.code, "AI_PROVIDER_RESPONSE_INVALID")
                    self.assertEqual(len(sink_calls), 1)
                    summary = engine.get_summary()
                    self.assertEqual(summary["total_calls"], 1)
                    self.assertEqual(summary["total_cost_usd"], 2.5)
                    self.assertEqual(summary["active_concurrency"], 0)
                    self.assertNotIn("sentinel", json.dumps(summary))

    def test_invalid_content_preserves_reported_usage_and_cost(self):
        invalid_envelopes = (
            {},
            {"choices": []},
            {"choices": [None]},
            {"choices": [{"message": None}]},
            {"choices": [{"message": {"content": " "}}]},
        )
        for provider_type, module in self._cases():
            for envelope in invalid_envelopes:
                with self.subTest(provider=provider_type.__name__, envelope=envelope):
                    engine = LLMTelemetryEngine(budget_ceiling_usd=None)
                    response = {**envelope, "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
                    provider = provider_type(client_factory=lambda **kwargs: httpx.Client(
                        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)),
                        **kwargs,
                    ))
                    with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                        with self.assertRaises(AIProviderError) as error:
                            self._generate(provider)
                    self.assertEqual(error.exception.code, "AI_PROVIDER_RESPONSE_INVALID")
                    summary = engine.get_summary()
                    self.assertEqual(summary["total_calls"], 1)
                    attempt = summary["recent_attempts"][0]
                    self.assertEqual(attempt["status"], "FAILED")
                    self.assertTrue(attempt["request_sent"])
                    self.assertEqual(attempt["prompt_tokens"], 1_000_000)
                    self.assertEqual(attempt["completion_tokens"], 0)
                    self.assertEqual(attempt["usage_provenance"], "PROVIDER_REPORTED")
                    self.assertEqual(attempt["estimated_cost"], 2.5)
                    self.assertEqual(summary["total_cost_usd"], 2.5)
                    self.assertNotIn("sentinel", json.dumps(summary))

    def test_invalid_content_preserves_only_valid_usage_fields(self):
        cases = (
            ({"prompt_tokens": 20}, 20, None, "PARTIAL"),
            ({"prompt_tokens": True, "completion_tokens": 3}, None, 3, "PARTIAL"),
            ({"prompt_tokens": -1, "completion_tokens": "3"}, None, None, "UNAVAILABLE"),
            (None, None, None, "UNAVAILABLE"),
        )
        for provider_type, module in self._cases():
            for usage, expected_prompt, expected_completion, provenance in cases:
                with self.subTest(provider=provider_type.__name__, usage=usage):
                    engine = LLMTelemetryEngine(budget_ceiling_usd=None)
                    provider = provider_type(client_factory=lambda **kwargs: httpx.Client(
                        transport=httpx.MockTransport(lambda _request: httpx.Response(
                            200, json={"choices": [], "usage": usage},
                        )), **kwargs,
                    ))
                    with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                        with self.assertRaises(AIProviderError):
                            self._generate(provider)
                    attempt = engine.get_summary()["recent_attempts"][0]
                    self.assertEqual(attempt["prompt_tokens"], expected_prompt)
                    self.assertEqual(attempt["completion_tokens"], expected_completion)
                    self.assertEqual(attempt["usage_provenance"], provenance)
                    self.assertIsNone(attempt["estimated_cost"])

    def test_invalid_json_usage_remains_unknown(self):
        body = b'{"usage":{"prompt_tokens":100},"usage":{"completion_tokens":2}}'
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                engine = LLMTelemetryEngine(budget_ceiling_usd=None)
                provider = provider_type(client_factory=lambda **kwargs: httpx.Client(
                    transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
                    **kwargs,
                ))
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    with self.assertRaises(AIProviderError) as error:
                        self._generate(provider)
                self.assertEqual(error.exception.code, "AI_PROVIDER_RESPONSE_INVALID")
                attempt = engine.get_summary()["recent_attempts"][0]
                self.assertTrue(attempt["request_sent"])
                self.assertEqual(attempt["usage_provenance"], "UNAVAILABLE")

    def test_failed_response_settles_budget_before_queued_request(self):
        for provider_type, module in self._cases():
            with self.subTest(provider=provider_type.__name__):
                first_entered = threading.Event()
                second_queued = threading.Event()
                release = threading.Event()
                requests = []
                errors = {}
                unexpected = []
                record_active_counts = []

                class ObservedEngine(LLMTelemetryEngine):
                    @contextmanager
                    def acquire_slot(self, provider, task="general", **kwargs):
                        if task == "waiting":
                            second_queued.set()
                        with super().acquire_slot(provider, task, **kwargs):
                            yield

                    def record_attempt(self, **kwargs):
                        if kwargs["task"] == "first":
                            record_active_counts.append(self.get_summary()["active_concurrency"])
                        return super().record_attempt(**kwargs)

                engine = ObservedEngine(global_concurrency_limit=1, budget_ceiling_usd=2.5)

                def handler(request):
                    requests.append(request)
                    first_entered.set()
                    if not release.wait(2):
                        raise RuntimeError("fixture was not released")
                    return httpx.Response(200, json={
                        "choices": [{"message": {"content": ""}}],
                        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
                    })

                provider = provider_type(client_factory=lambda **kwargs: httpx.Client(
                    transport=httpx.MockTransport(handler), **kwargs,
                ))

                def call(task):
                    try:
                        provider.generate(task=task, model="gpt-4o", prompt="synthetic", timeout_seconds=3)
                    except AIProviderError as error:
                        errors[task] = error.code
                    except Exception as error:
                        unexpected.append(type(error).__name__)

                first = threading.Thread(target=call, args=("first",), daemon=True)
                second = threading.Thread(target=call, args=("waiting",), daemon=True)
                with patch(f"{module}.get_default_telemetry_engine", return_value=engine):
                    first.start()
                    try:
                        self.assertTrue(first_entered.wait(1))
                        second.start()
                        self.assertTrue(second_queued.wait(1))
                    finally:
                        release.set()
                        first.join(2)
                        if second.ident is not None:
                            second.join(2)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(unexpected, [])
                self.assertEqual(errors, {
                    "first": "AI_PROVIDER_RESPONSE_INVALID",
                    "waiting": "AI_PROVIDER_BUDGET_EXCEEDED",
                })
                self.assertEqual(record_active_counts, [1])
                self.assertEqual(len(requests), 1)
                summary = engine.get_summary()
                self.assertEqual(summary["total_calls"], 2)
                self.assertEqual(summary["total_cost_usd"], 2.5)
                self.assertEqual(summary["active_concurrency"], 0)
                self.assertEqual(summary["unknown_cost_calls"], 0)


if __name__ == "__main__":
    unittest.main()
