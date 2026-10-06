"""The Ollama provider is loopback-only, bounded, and synthetic-testable."""
import json
import unittest
from unittest.mock import patch

import httpx


class OllamaProviderTests(unittest.TestCase):
    def test_factory_registers_local_provider_without_changing_disabled_defaults(self):
        from services.ai.providers import (
            DisabledAIProvider, OllamaProvider, ProviderCapabilities, create_provider,
        )
        from services.ai.tasks import AITaskConfiguration

        local = create_provider("ollama")

        self.assertIsInstance(local, OllamaProvider)
        self.assertEqual(local.name, "ollama")
        self.assertEqual(local.capabilities,
                         ProviderCapabilities(text_generation=True, structured_output=True))
        self.assertIsInstance(create_provider("disabled"), DisabledAIProvider)
        self.assertTrue(all(AITaskConfiguration.default().for_task(task).provider == "disabled"
                            for task in AITaskConfiguration.default().tasks))

    def test_endpoint_must_be_a_loopback_http_origin(self):
        from services.ai.providers import AIProviderError, OllamaProvider

        for url in ("http://example.com:11434", "http://192.168.1.10:11434",
                    "https://127.0.0.1:11434", "http://127.0.0.1:11434/api",
                    "http://user:pass@127.0.0.1:11434", "http://127.0.0.1:11434?x=1",
                    "http://127.0.0.1:11434/#fragment"):
            with self.subTest(url=url), self.assertRaises(AIProviderError) as error:
                OllamaProvider(base_url=url)
            self.assertEqual(error.exception.code, "AI_PROVIDER_CONFIGURATION_INVALID")

        self.assertEqual(OllamaProvider(base_url="http://127.0.0.1:11434").endpoint,
                         "http://127.0.0.1:11434/api/generate")
        self.assertEqual(OllamaProvider(base_url="http://[::1]:11434").endpoint,
                         "http://[::1]:11434/api/generate")

    def test_factory_validates_loopback_environment_endpoint(self):
        from services.ai.providers import AIProviderError, OllamaProvider, create_provider

        with patch.dict("os.environ", {"MJOBS_OLLAMA_BASE_URL": "http://127.0.0.1:12000"}):
            provider = create_provider("ollama")
        self.assertIsInstance(provider, OllamaProvider)
        self.assertEqual(provider.endpoint, "http://127.0.0.1:12000/api/generate")

        with patch.dict("os.environ", {"MJOBS_OLLAMA_BASE_URL": "http://example.com"}):
            with self.assertRaises(AIProviderError) as error:
                create_provider("ollama")
        self.assertEqual(error.exception.code, "AI_PROVIDER_CONFIGURATION_INVALID")

    def test_non_streaming_request_forwards_model_prompt_and_json_schema(self):
        from services.ai.providers import OllamaProvider

        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={
                "model": "llama3.2",
                "response": '{"score":87}',
                "done": True,
            })

        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            client_factory=lambda **kwargs: httpx.Client(
                transport=httpx.MockTransport(respond), **kwargs,
            ),
        )
        schema = {"type": "object", "properties": {"score": {"type": "integer"}},
                  "required": ["score"], "additionalProperties": False}

        result = provider.generate(
            task="job_scoring", model="llama3.2", prompt="synthetic only",
            timeout_seconds=2, response_schema=schema,
        )

        self.assertEqual(result, '{"score":87}')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url, "http://127.0.0.1:11434/api/generate")
        body = json.loads(requests[0].content)
        self.assertEqual(body, {
            "model": "llama3.2", "prompt": "synthetic only", "stream": False,
            "format": schema,
        })

    def test_task_router_and_local_provider_complete_a_synthetic_structured_roundtrip(self):
        from services.ai.providers import OllamaProvider
        from services.ai.structured_output import StrictAIResult
        from services.ai.tasks import AITaskConfiguration, AITaskRouter

        class Score(StrictAIResult):
            score: int

        provider = OllamaProvider(client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
                "model": "llama3.2", "response": '{"score":94}', "done": True,
            })),
            **kwargs,
        ))
        router = AITaskRouter(
            AITaskConfiguration.from_mapping({
                "job_scoring": {"provider": "ollama", "model": "llama3.2"},
            }),
            provider_factory=lambda name: provider,
        )

        result = router.generate_structured(
            task="job_scoring", prompt="synthetic prompt", timeout_seconds=1,
            result_model=Score,
        )

        self.assertIsInstance(result, Score)
        self.assertEqual(result.score, 94)

    def test_text_request_omits_schema_and_provider_does_not_use_environment_proxies(self):
        from services.ai.providers import OllamaProvider

        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"response": "synthetic text", "done": True})

        def client_factory(**kwargs):
            self.assertIs(kwargs["trust_env"], False)
            return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)

        provider = OllamaProvider(client_factory=client_factory)
        result = provider.generate(task="cover_letters", model="llama3.2",
                                   prompt="synthetic prompt", timeout_seconds=1)

        self.assertEqual(result, "synthetic text")
        self.assertNotIn("format", json.loads(requests[0].content))

    def test_invalid_request_values_fail_before_transport(self):
        from services.ai.providers import AIProviderError, OllamaProvider

        calls = []
        provider = OllamaProvider(client_factory=lambda **kwargs: calls.append(kwargs))
        invalid = (
            {"task": "bad task", "model": "llama3.2", "prompt": "x", "timeout_seconds": 1},
            {"task": "job_scoring", "model": "", "prompt": "x", "timeout_seconds": 1},
            {"task": "job_scoring", "model": "llama3.2", "prompt": "", "timeout_seconds": 1},
            {"task": "job_scoring", "model": "llama3.2", "prompt": "x", "timeout_seconds": True},
            {"task": "job_scoring", "model": "llama3.2", "prompt": "x", "timeout_seconds": 301},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(AIProviderError) as error:
                provider.generate(**kwargs)
            self.assertEqual(error.exception.code, "AI_PROVIDER_REQUEST_INVALID")
        self.assertEqual(calls, [])

    def test_invalid_response_body_and_oversized_content_fail_with_stable_codes(self):
        from services.ai.providers import AIProviderError, OllamaProvider
        from services.ai.providers.ollama import MAX_RESPONSE_BYTES

        for body, expected_code in (
            (b'{"response":"PRIVATE_RESPONSE","done":true,"done":false}',
             "AI_PROVIDER_RESPONSE_INVALID"),
            (b'{"response":12,"done":true}', "AI_PROVIDER_RESPONSE_INVALID"),
            (b'{"response":"incomplete","done":false}', "AI_PROVIDER_RESPONSE_INVALID"),
            (b'{"error":"PRIVATE_RESPONSE"}', "AI_PROVIDER_RESPONSE_INVALID"),
            (b"not-json", "AI_PROVIDER_RESPONSE_INVALID"),
            (b"\xff", "AI_PROVIDER_RESPONSE_INVALID"),
            (b"x" * (MAX_RESPONSE_BYTES + 1), "AI_PROVIDER_RESPONSE_TOO_LARGE"),
        ):
            provider = OllamaProvider(client_factory=lambda body=body, **kwargs: httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
                **kwargs,
            ))
            with self.subTest(body=body), self.assertRaises(AIProviderError) as error:
                provider.generate(task="job_scoring", model="llama3.2", prompt="private",
                                  timeout_seconds=1)
            self.assertEqual(error.exception.code, expected_code)
            self.assertNotIn("PRIVATE_RESPONSE", str(error.exception))

    def test_http_and_transport_failures_do_not_echo_private_content(self):
        from services.ai.providers import AIProviderError, OllamaProvider

        http_provider = OllamaProvider(client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(
                500, content=b"PRIVATE_SERVER_BODY", request=request,
            )),
            **kwargs,
        ))
        with self.assertRaises(AIProviderError) as http_error:
            http_provider.generate(task="job_scoring", model="llama3.2",
                                   prompt="PRIVATE_PROMPT", timeout_seconds=1)
        self.assertEqual(http_error.exception.code, "AI_PROVIDER_HTTP_ERROR")
        self.assertNotIn("PRIVATE_SERVER_BODY", str(http_error.exception))
        self.assertNotIn("PRIVATE_PROMPT", str(http_error.exception))

        def fail(request):
            raise httpx.ConnectError("PRIVATE_PROMPT PRIVATE_ENDPOINT", request=request)

        transport_provider = OllamaProvider(client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(fail), **kwargs,
        ))
        with self.assertRaises(AIProviderError) as transport_error:
            transport_provider.generate(task="job_scoring", model="llama3.2",
                                        prompt="PRIVATE_PROMPT", timeout_seconds=1)
        self.assertEqual(transport_error.exception.code, "AI_PROVIDER_UNAVAILABLE")
        self.assertNotIn("PRIVATE_ENDPOINT", str(transport_error.exception))


if __name__ == "__main__":
    unittest.main()
