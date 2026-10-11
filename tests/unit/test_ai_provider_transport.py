"""Provider credentials and private prompts require encrypted remote transport."""
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import httpx

from services.ai.providers import (
    AIProviderError, AzureOpenAIProvider, OpenAICompatibleProvider, OpenAIProvider,
)


PROVIDERS = (OpenAICompatibleProvider, OpenAIProvider, AzureOpenAIProvider)


class AIProviderTransportTests(unittest.TestCase):
    def test_remote_http_is_rejected_before_client_creation(self):
        for provider_type in PROVIDERS:
            for url in (
                'http://api.example.org/v1', 'http://192.168.1.20:8080/v1',
                'http://169.254.169.254/v1', 'http://[2001:db8::1]/v1',
                'http://localhost/v1', 'http://127.0.0.1.attacker.example/v1',
                'http://127.1/v1', 'http://2130706433/v1',
            ):
                factory = Mock()
                with self.subTest(provider=provider_type.name, url=url):
                    with self.assertRaises(AIProviderError) as error:
                        provider_type(base_url=url, api_key='synthetic-key', client_factory=factory)
                    self.assertEqual(error.exception.code, 'AI_PROVIDER_CONFIGURATION_INVALID')
                    self.assertNotIn(url, str(error.exception))
                    factory.assert_not_called()

    def test_controls_whitespace_and_backslash_are_not_normalized_into_an_endpoint(self):
        for provider_type in PROVIDERS:
            for url in (
                '\nhttps://api.example.org/v1', ' https://api.example.org/v1',
                'https://api.exa\tmple.org/v1', 'https://api.example.org/v1\r',
                'https://api.example.org/with space', 'https://api.example.org/\\path',
                'https://api.example.org/v1\x00',
            ):
                with self.subTest(provider=provider_type.name, url=repr(url)):
                    with self.assertRaises(AIProviderError):
                        provider_type(base_url=url, api_key='synthetic-key')

    def test_https_and_literal_loopback_http_still_generate(self):
        for provider_type in PROVIDERS:
            for url in ('https://api.example.org/prefix/v1/',
                        'http://127.0.0.1:8100/v1', 'http://[::1]:8100/v1'):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})

                def factory(**kwargs):
                    return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

                with self.subTest(provider=provider_type.name, url=url):
                    provider = provider_type(base_url=url, api_key='synthetic-key', client_factory=factory)
                    self.assertEqual(provider.generate(task='test', model='fixture-model',
                        prompt='synthetic prompt', timeout_seconds=1), 'ok')
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].url.scheme, url.split(':')[0])
                    suffix = ('/openai/deployments/fixture-model/chat/completions'
                        if provider_type is AzureOpenAIProvider else '/chat/completions')
                    self.assertEqual(requests[0].url.path, urlsplit(url).path.rstrip('/') + suffix)
                    header = 'api-key' if provider_type is AzureOpenAIProvider else 'authorization'
                    self.assertIn('synthetic-key', requests[0].headers[header])
                    self.assertIn(b'synthetic prompt', requests[0].content)

    def test_environment_configuration_uses_same_transport_boundary(self):
        for provider_type, name in (
            (OpenAICompatibleProvider, 'MJOBS_OPENAI_COMPATIBLE_BASE_URL'),
            (OpenAIProvider, 'MJOBS_OPENAI_BASE_URL'),
            (AzureOpenAIProvider, 'AZURE_OPENAI_ENDPOINT'),
        ):
            with self.subTest(provider=provider_type.name), patch.dict('os.environ',
                    {name: 'http://remote.example/v1'}, clear=True):
                with self.assertRaises(AIProviderError):
                    provider_type.from_environment()

    def test_redirect_cannot_replay_private_prompt_to_another_origin(self):
        for provider_type in PROVIDERS:
            for destination in ('http://api.example.org/stolen', 'https://other.example/stolen'):
                requests = []

                def handler(request):
                    requests.append(request)
                    if len(requests) == 1:
                        return httpx.Response(307, headers={'Location': destination})
                    return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})

                def factory(**kwargs):
                    return httpx.Client(transport=httpx.MockTransport(handler),
                        follow_redirects=True, **kwargs)

                with self.subTest(provider=provider_type.name, destination=destination):
                    provider = provider_type(base_url='https://api.example.org/v1',
                        api_key='synthetic-key', client_factory=factory)
                    with self.assertRaises(AIProviderError) as error:
                        provider.generate(task='test', model='fixture-model',
                            prompt='synthetic private prompt', timeout_seconds=1)
                    self.assertEqual(error.exception.code, 'AI_PROVIDER_HTTP_ERROR')
                    self.assertEqual(len(requests), 1)
