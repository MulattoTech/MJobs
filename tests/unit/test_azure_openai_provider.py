"""Contract and unit tests for Azure OpenAI provider (MJ-REQ-108.A03)."""

import json
import threading
import unittest
from unittest.mock import patch

import httpx

from services.ai.providers import (
    AIProviderError,
    AzureOpenAIProvider,
    ProviderCapabilities,
    create_provider,
)
from services.ai.telemetry import get_default_telemetry_engine


class AzureOpenAIProviderTests(unittest.TestCase):
    def test_factory_creates_azure_provider(self):
        p1 = create_provider("azure-openai")
        self.assertIsInstance(p1, AzureOpenAIProvider)
        self.assertEqual(p1.name, "azure-openai")

        p2 = create_provider("azure")
        self.assertIsInstance(p2, AzureOpenAIProvider)

    def test_endpoint_construction_with_deployment_and_api_version(self):
        provider = AzureOpenAIProvider(
            base_url="https://contoso-corp.openai.azure.com",
            api_key="azure-secret-key",
            api_version="2024-02-15-preview",
        )
        endpoint = provider.get_deployment_endpoint("gpt-4o-deployment")
        expected = "https://contoso-corp.openai.azure.com/openai/deployments/gpt-4o-deployment/chat/completions?api-version=2024-02-15-preview"
        self.assertEqual(endpoint, expected)

    def test_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "AZURE_OPENAI_ENDPOINT": "https://custom-env.openai.azure.com",
                "AZURE_OPENAI_API_KEY": "env-key-999",
                "AZURE_OPENAI_API_VERSION": "2024-06-01",
            },
        ):
            provider = AzureOpenAIProvider.from_environment()
            self.assertEqual(provider.base_url, "https://custom-env.openai.azure.com")
            self.assertEqual(provider._api_key, "env-key-999")
            self.assertEqual(provider.api_version, "2024-06-01")

    def test_successful_generation_and_auth_header(self):
        captured_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "  Azure AI generated match analysis  ",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 80,
                        "completion_tokens": 25,
                    },
                },
            )

        provider = AzureOpenAIProvider(
            base_url="https://test.openai.azure.com",
            api_key="my-azure-key",
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
        )

        telemetry = get_default_telemetry_engine()
        initial_calls = telemetry.get_summary()["total_calls"]

        result = provider.generate(
            task="job_scoring",
            model="gpt-4o",
            prompt="Analyze candidate fit",
            timeout_seconds=15.0,
        )

        self.assertEqual(result, "Azure AI generated match analysis")
        self.assertEqual(len(captured_requests), 1)

        req = captured_requests[0]
        # Azure OpenAI uses api-key header
        self.assertEqual(req.headers.get("api-key"), "my-azure-key")
        self.assertIn("openai/deployments/gpt-4o/chat/completions", str(req.url))

        summary = telemetry.get_summary()
        self.assertEqual(summary["total_calls"], initial_calls + 1)

    def test_structured_output_format(self):
        captured_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"verdict": "MATCH"}'}}]},
            )

        provider = AzureOpenAIProvider(
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
        )

        result = provider.generate(
            task="scoring",
            model="gpt-4o",
            prompt="Output JSON",
            timeout_seconds=10.0,
            response_schema={"type": "object", "properties": {"verdict": {"type": "string"}}},
        )

        self.assertEqual(result, '{"verdict": "MATCH"}')
        payload = json.loads(captured_requests[0].content.decode("utf-8"))
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_http_error_handling(self):
        def error_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized: Invalid API Key")

        provider = AzureOpenAIProvider(
            client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(error_handler), **kw)
        )

        with self.assertRaises(AIProviderError) as err:
            provider.generate(task="task", model="m", prompt="p", timeout_seconds=5)
        self.assertEqual(err.exception.code, "AI_PROVIDER_HTTP_ERROR")

    def test_cancellation(self):
        cancel_event = threading.Event()
        cancel_event.set()

        provider = AzureOpenAIProvider()
        with self.assertRaises(AIProviderError) as err:
            provider.generate(
                task="task",
                model="m",
                prompt="p",
                timeout_seconds=5,
                cancellation_event=cancel_event,
            )
        self.assertEqual(err.exception.code, "AI_PROVIDER_CANCELLED")


if __name__ == "__main__":
    unittest.main()
