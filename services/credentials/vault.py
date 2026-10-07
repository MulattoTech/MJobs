"""Versioned authenticated encryption for profile-scoped provider credentials."""
from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_ENVELOPE = b"MJCR1"
_NONCE_BYTES = 12


class CredentialConfigurationError(ValueError):
    """Safe, value-free credential key or ciphertext error."""

    code = "CREDENTIAL_STORE_UNAVAILABLE"

    def __init__(self, code: str = "CREDENTIAL_STORE_UNAVAILABLE") -> None:
        self.code = code
        super().__init__(code)


class CredentialCipher:
    """AES-256-GCM with profile/provider identity authenticated as associated data."""

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != 32:
            raise CredentialConfigurationError()
        self._cipher = AESGCM(key)

    @classmethod
    def from_key_material(cls, value: str | None) -> "CredentialCipher":
        if not isinstance(value, str) or not value or len(value) > 128:
            raise CredentialConfigurationError()
        try:
            key = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeError, binascii.Error, ValueError):
            raise CredentialConfigurationError() from None
        return cls(key)

    @classmethod
    def from_environment(cls) -> "CredentialCipher":
        return cls.from_key_material(os.environ.get("MJOBS_CREDENTIAL_ENCRYPTION_KEY"))

    @staticmethod
    def _aad(profile_id: str, provider: str) -> bytes:
        return b"mjobs-provider-credential-v1\0" + profile_id.encode() + b"\0" + provider.encode()

    def encrypt(self, profile_id: str, provider: str, secret: str) -> bytes:
        if (not isinstance(profile_id, str) or not profile_id
                or not isinstance(provider, str) or not provider
                or not isinstance(secret, str) or not secret or len(secret) > 16_384):
            raise CredentialConfigurationError("CREDENTIAL_INPUT_INVALID")
        nonce = os.urandom(_NONCE_BYTES)
        encrypted = self._cipher.encrypt(nonce, secret.encode("utf-8"), self._aad(profile_id, provider))
        return _ENVELOPE + nonce + encrypted

    def decrypt(self, profile_id: str, provider: str, encrypted: bytes) -> str:
        if not isinstance(encrypted, bytes) or len(encrypted) <= len(_ENVELOPE) + _NONCE_BYTES + 16:
            raise CredentialConfigurationError()
        if not encrypted.startswith(_ENVELOPE):
            raise CredentialConfigurationError()
        nonce = encrypted[len(_ENVELOPE):len(_ENVELOPE) + _NONCE_BYTES]
        ciphertext = encrypted[len(_ENVELOPE) + _NONCE_BYTES:]
        try:
            return self._cipher.decrypt(nonce, ciphertext, self._aad(profile_id, provider)).decode("utf-8")
        except (InvalidTag, UnicodeError, ValueError):
            raise CredentialConfigurationError() from None
