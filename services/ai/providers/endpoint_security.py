"""Validate configured provider transport before credentials or prompts are sent."""
import ipaddress
from urllib.parse import urlsplit

from services.ai.providers import AIProviderError


def secure_base_url(value: object) -> str:
    """Allow remote HTTPS and canonical literal loopback HTTP, without DNS lookup."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048 or
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == '\\' for c in value)):
        raise AIProviderError('AI_PROVIDER_CONFIGURATION_INVALID')
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except (ValueError, TypeError):
        raise AIProviderError('AI_PROVIDER_CONFIGURATION_INVALID') from None
    if (parsed.scheme not in ('http', 'https') or not host or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or
            (port is not None and not 1 <= port <= 65535)):
        raise AIProviderError('AI_PROVIDER_CONFIGURATION_INVALID')
    if parsed.scheme == 'http':
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise AIProviderError('AI_PROVIDER_CONFIGURATION_INVALID') from None
        if not address.is_loopback:
            raise AIProviderError('AI_PROVIDER_CONFIGURATION_INVALID')
    return f'{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip("/")}'
