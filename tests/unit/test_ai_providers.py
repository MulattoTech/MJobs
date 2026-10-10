"""Contract and unit tests for OpenAI, OpenAI-compatible, and Local AI providers."""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

import httpx

from services.ai.providers import (
    AIProviderError,
    DisabledAIProvider,
    LocalEndpointProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderCapabilities,
    create_provider,
)
from services.ai.telemetry import get_default_telemetry_engine


class AIProvidersFactoryTests(unittest.TestCase):
    def test_factory_creates_all_supported_providers(self):
        self.assertIsInstance(create_provider("disabled"), DisabledAIProvider)
        self.assertIsInstance(create_provider("ollama"), OllamaProvider)
        self.assertIsInstance(create_provider("openai"), OpenAIProvider)
        self.assertIsInstance(create_provider("openai-compatible"), OpenAICompatibleProvider)
        self.assertIsInstance(create_provider("local"), LocalEndpointProvider)

    def test_factory_rejects_unknown_provider(self):
        with self.assertRaises(ValueError) as error:
            create_provider("unknown-ai-system")
        self.assertEqual(str(error.exception), "AI_PROVIDER_UNSUPPORTED")


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_url_sanitization_and_endpoint(self):
        # Valid URLs
        p1 = OpenAICompatibleProvider(base_url="http://127.0.0.1:8000/v1")
        self.assertEqual(p1.endpoint, "http://127.0.0.1:8000/v1/chat/completions")

        p2 = OpenAICompatibleProvider(base_url="https://api.custom-ai.org/v1/")
        self.assertEqual(p2.endpoint, "https://api.custom-ai.org/v1/chat/completions")

        # Invalid URLs
        for bad_url in (
            "",
            "ftp://127.0.0.1:8000",
            "http://user:pass@127.0.0.1:8000",
            "http://127.0.0.1:8000?query=1",
            "http://127.0.0.1:8000#fragment",
            "http://127.0.0.1:999999",
        ):
            with self.subTest(bad_url=bad_url), self.assertRaises(AIProviderError) as err:
                OpenAICompatibleProvider(base_url=bad_url)
            self.assertEqual(err.exception.code, "AI_PROVIDER_CONFIGURATION_INVALID")

    def test_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "MJOBS_OPENAI_COMPATIBLE_BASE_URL": "http://127.0.0.1:9000/v1",
                "MJOBS_OPENAI_COMPATIBLE_API_KEY": "test-env-key",
            },
        ):
            provider = OpenAICompatibleProvider.from_environment()
            self.assertEqual(provider.endpoint, "http://127.0.0.1:9000/v1/chat/completions")
            self.assertEqual(provider._api_key, "test-env-key")

    def test_successful_generation_and_telemetry(self):
        captured_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            body = {
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "  Qualified candidate match score 92%  ",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 150,
                    "completion_tokens": 42,
                    "total_tokens": 192,
                },
            }
            return httpx.Response(200, json=body)

        mock_client = lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        )
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:8000/v1",
            api_key="secret-key-123",
            client_factory=mock_client,
        )

        telemetry = get_default_telemetry_engine()
        initial_calls = telemetry.get_summary()["total_calls"]

        result = provider.generate(
            task="job_scoring",
            model="gpt-4o-mini",
            prompt="Evaluate candidate seniority",
            timeout_seconds=30.0,
        )

        self.assertEqual(result, "Qualified candidate match score 92%")
        self.assertEqual(len(captured_requests), 1)

        req = captured_requests[0]
        self.assertEqual(req.headers["authorization"], "Bearer secret-key-123")
        payload = json.loads(req.content.decode("utf-8"))
        self.assertEqual(payload["model"], "gpt-4o-mini")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Evaluate candidate seniority"}])
        self.assertFalse(payload["stream"])

        # Check telemetry recorded the attempt
        summary = telemetry.get_summary()
        self.assertEqual(summary["total_calls"], initial_calls + 1)

    def test_structured_output_format(self):
        captured_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"score": 95}'}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                },
            )

        provider = OpenAICompatibleProvider(
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
        )

        result = provider.generate(
            task="scoring",
            model="local-model",
            prompt="Return JSON",
            timeout_seconds=10.0,
            response_schema={"type": "object", "properties": {"score": {"type": "number"}}},
        )

        self.assertEqual(result, '{"score": 95}')
        payload = json.loads(captured_requests[0].content.decode("utf-8"))
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_request_validation(self):
        provider = OpenAICompatibleProvider()

        # Bad task
        with self.assertRaises(AIProviderError) as err:
            provider.generate(task="INVALID TASK!", model="m", prompt="p", timeout_seconds=10)
        self.assertEqual(err.exception.code, "AI_PROVIDER_REQUEST_INVALID")

        # Bad model
        with self.assertRaises(AIProviderError) as err:
            provider.generate(task="task", model="", prompt="p", timeout_seconds=10)
        self.assertEqual(err.exception.code, "AI_PROVIDER_REQUEST_INVALID")

        # Empty prompt
        with self.assertRaises(AIProviderError) as err:
            provider.generate(task="task", model="m", prompt="   ", timeout_seconds=10)
        self.assertEqual(err.exception.code, "AI_PROVIDER_REQUEST_INVALID")

        # Bad timeout
        with self.assertRaises(AIProviderError) as err:
            provider.generate(task="task", model="m", prompt="p", timeout_seconds=0)
        self.assertEqual(err.exception.code, "AI_PROVIDER_REQUEST_INVALID")

    def test_http_error_handling(self):
        def error_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        provider = OpenAICompatibleProvider(
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(error_handler), **kw)
        )

        with self.assertRaises(AIProviderError) as err:
            provider.generate(task="task", model="m", prompt="p", timeout_seconds=5)
        self.assertEqual(err.exception.code, "AI_PROVIDER_HTTP_ERROR")

    def test_cancellation(self):
        cancel_event = threading.Event()
        cancel_event.set()

        provider = OpenAICompatibleProvider()
        with self.assertRaises(AIProviderError) as err:
            provider.generate(
                task="task",
                model="m",
                prompt="p",
                timeout_seconds=5,
                cancellation_event=cancel_event,
            )
        self.assertEqual(err.exception.code, "AI_PROVIDER_CANCELLED")


class OpenAIProviderTests(unittest.TestCase):
    def test_default_properties_and_api_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-proj-test12345"}):
            provider = OpenAIProvider()
            self.assertEqual(provider.name, "openai")
            self.assertEqual(provider.endpoint, "https://api.openai.com/v1/chat/completions")
            self.assertEqual(provider._api_key, "sk-proj-test12345")
            self.assertEqual(
                provider.capabilities,
                ProviderCapabilities(text_generation=True, structured_output=True, streaming=False),
            )


class LocalEndpointProviderTests(unittest.TestCase):
    def test_loopback_validation(self):
        # Valid loopback
        p1 = LocalEndpointProvider(base_url="http://127.0.0.1:8080/v1")
        self.assertEqual(p1.endpoint, "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(p1.name, "local")

        p2 = LocalEndpointProvider(base_url="http://[::1]:5000/v1")
        self.assertEqual(p2.endpoint, "http://[::1]:5000/v1/chat/completions")

        # Non-loopback IPs or external domain must be rejected (SSRF protection)
        for external_url in (
            "http://example.com:8080/v1",
            "http://192.168.1.100:8080/v1",
            "http://10.0.0.1:8080/v1",
            "https://127.0.0.1:8080/v1",  # scheme must be http for local
        ):
            with self.subTest(external_url=external_url), self.assertRaises(AIProviderError) as err:
                LocalEndpointProvider(base_url=external_url)
            self.assertEqual(err.exception.code, "AI_PROVIDER_CONFIGURATION_INVALID")

    def test_local_endpoint_generation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Local model output"}}]},
            )

        provider = LocalEndpointProvider(
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
        )
        result = provider.generate(
            task="matching",
            model="mistral-7b-instruct",
            prompt="Hello from local",
            timeout_seconds=10.0,
        )
        self.assertEqual(result, "Local model output")


if __name__ == "__main__":
    unittest.main()
