"""Credential encryption and fixed-origin connection probes."""
import base64
import unittest

import httpx

from services.credentials.connection import test_provider_connection as probe_provider_connection
from services.credentials.vault import CredentialCipher, CredentialConfigurationError


class CredentialCipherTests(unittest.TestCase):
    def setUp(self):
        self.key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
        self.cipher = CredentialCipher.from_key_material(self.key)

    def test_encrypts_and_authenticates_profile_provider_associated_data(self):
        secret = "synthetic-provider-secret"
        encrypted = self.cipher.encrypt("profile-a", "openai", secret)
        self.assertIsInstance(encrypted, bytes)
        self.assertNotIn(secret.encode(), encrypted)
        self.assertEqual(self.cipher.decrypt("profile-a", "openai", encrypted), secret)
        with self.assertRaises(CredentialConfigurationError):
            self.cipher.decrypt("profile-b", "openai", encrypted)
        with self.assertRaises(CredentialConfigurationError):
            self.cipher.decrypt("profile-a", "anthropic", encrypted)

    def test_rejects_tampered_ciphertext_and_invalid_key_material(self):
        encrypted = self.cipher.encrypt("profile-a", "openai", "synthetic-provider-secret")
        with self.assertRaises(CredentialConfigurationError):
            self.cipher.decrypt("profile-a", "openai", encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
        for value in (None, "", "not-base64", base64.urlsafe_b64encode(b"short").decode("ascii")):
            with self.subTest(value=value):
                with self.assertRaises(CredentialConfigurationError):
                    CredentialCipher.from_key_material(value)


class ProviderConnectionTests(unittest.TestCase):
    def test_connection_probe_uses_fixed_provider_origin_and_never_returns_secret(self):
        secret = "synthetic-openai-secret"
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": []})

        def client_factory(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        result = probe_provider_connection("openai", secret, client_factory=client_factory)
        self.assertEqual(result, {"status": "CONNECTED", "code": None})
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://api.openai.com/v1/models")
        self.assertEqual(requests[0].headers["authorization"], f"Bearer {secret}")
        self.assertNotIn(secret, repr(result))

    def test_rejected_connection_has_sanitized_outcome(self):
        def handler(_request):
            return httpx.Response(401, text="synthetic-secret-reflected-by-server")

        def client_factory(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        result = probe_provider_connection("anthropic", "synthetic-anthropic-secret", client_factory=client_factory)
        self.assertEqual(result, {"status": "FAILED", "code": "CREDENTIAL_REJECTED"})
        self.assertNotIn("synthetic-secret-reflected-by-server", repr(result))

    def test_unknown_provider_is_rejected_before_network_access(self):
        def fail_if_called(**_kwargs):
            raise AssertionError("unknown provider attempted network access")

        result = probe_provider_connection("https://127.0.0.1/private", "synthetic-secret",
                                          client_factory=fail_if_called)
        self.assertEqual(result, {"status": "FAILED", "code": "CREDENTIAL_PROVIDER_UNSUPPORTED"})


if __name__ == "__main__":
    unittest.main()
