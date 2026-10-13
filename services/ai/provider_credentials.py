"""Bind explicit AI work to profile credentials without persisting plaintext.

Saved OpenAI keys have one fixed destination. Existing runtime-configured plans
keep their original source; adding a saved key never changes an admitted plan.
"""
import hashlib
import re

from sqlalchemy import select

from packages.persistence import models as m
from services.ai.providers.openai import OpenAIProvider, DEFAULT_OPENAI_BASE_URL
from services.credentials.vault import CredentialCipher, CredentialConfigurationError


class CredentialBindingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def snapshot_binding(store, profile_id, provider):
    if provider == 'openai':
        encrypted = store.encrypted_provider_credential(profile_id, provider)
        if encrypted is not None:
            return {'provider': provider, 'source': 'SAVED_PROFILE',
                    'ciphertext_fingerprint': hashlib.sha256(encrypted).hexdigest()}
    return {'provider': provider,
            'source': 'DISABLED' if provider == 'disabled' else 'RUNTIME_CONFIGURATION'}


def public_access(binding):
    if binding is None:
        return None  # Legacy plans never claimed a saved credential binding.
    return {'provider': binding['provider'], 'credential_source': binding['source'],
            'destination': 'https://api.openai.com' if binding['source'] == 'SAVED_PROFILE' else None}


def require_binding(connection, profile_id, snapshot):
    binding = snapshot.get('provider_binding')
    if binding is None:
        return None
    provider = snapshot['configuration']['job_scoring']['provider']
    if (not isinstance(binding, dict) or binding.get('provider') != provider
            or binding.get('source') not in ('SAVED_PROFILE', 'RUNTIME_CONFIGURATION', 'DISABLED')):
        raise CredentialBindingError('AI_PROVIDER_BINDING_INVALID')
    if (binding['source'] == 'DISABLED') != (provider == 'disabled'):
        raise CredentialBindingError('AI_PROVIDER_BINDING_INVALID')
    if binding['source'] != 'SAVED_PROFILE':
        return None
    fingerprint = binding.get('ciphertext_fingerprint')
    if provider != 'openai' or not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
        raise CredentialBindingError('AI_PROVIDER_BINDING_INVALID')
    encrypted = connection.execute(select(m.provider_credentials.c.encrypted_secret).where(
        m.provider_credentials.c.profile_id == profile_id,
        m.provider_credentials.c.provider == provider).with_for_update()).scalar_one_or_none()
    if encrypted is None or hashlib.sha256(encrypted).hexdigest() != fingerprint:
        raise CredentialBindingError('AI_PROVIDER_CREDENTIAL_CHANGED')
    return encrypted


def bound_provider_factory(store, profile_id, snapshot):
    """Resolve only the admitted saved key, before any CALL_STARTED marker."""
    binding = snapshot.get('provider_binding')
    if not binding or binding.get('source') != 'SAVED_PROFILE':
        return None
    with store.engine.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        encrypted = require_binding(connection, profile_id, snapshot)
        try:
            secret = CredentialCipher.from_environment().decrypt(profile_id, 'openai', encrypted)
        except CredentialConfigurationError:
            raise CredentialBindingError('AI_PROVIDER_CREDENTIAL_UNAVAILABLE') from None
    # Never use an environment URL override or another profile's credentials.
    provider = OpenAIProvider(api_key=secret, base_url=DEFAULT_OPENAI_BASE_URL)

    def create(name):
        if name != 'openai':
            raise CredentialBindingError('AI_PROVIDER_BINDING_INVALID')
        return provider

    return create
