"""The no-AI provider must fail closed and declare exactly zero capabilities."""
import importlib.util
import unittest
from unittest.mock import patch


class DisabledAIProviderTests(unittest.TestCase):
    def test_disabled_provider_is_explicit_and_never_generates(self):
        spec = importlib.util.find_spec('services.ai')
        self.assertIsNotNone(spec, 'the concrete AI provider package is missing')
        if spec is None:
            return
        from services.ai import providers

        provider=providers.create_provider('disabled')
        self.assertEqual(provider.name, 'disabled')
        self.assertEqual(provider.capabilities, providers.ProviderCapabilities())
        with patch('socket.create_connection', side_effect=AssertionError('network access is forbidden')):
            with self.assertRaises(providers.AIProviderDisabled) as error:
                provider.generate(task='job_scoring', prompt='PRIVATE INPUT', timeout_seconds=0.1)
        self.assertEqual(error.exception.code, 'AI_PROVIDER_DISABLED')
        self.assertNotIn('PRIVATE INPUT', str(error.exception))

    def test_factory_rejects_unknown_provider_without_echoing_input(self):
        from services.ai import providers

        with self.assertRaises(ValueError) as error:
            providers.create_provider('secret-provider-config')
        self.assertEqual(str(error.exception), 'AI_PROVIDER_UNSUPPORTED')
        self.assertNotIn('secret-provider-config', str(error.exception))


if __name__=='__main__':
    unittest.main()
