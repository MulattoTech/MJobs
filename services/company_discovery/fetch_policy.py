"""Fail-closed URL policy for company-site discovery transports."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def _origin(url, fixture_only=False):
    try:
        parsed = urlsplit(str(url or ""))
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        host = parsed.hostname.casefold().rstrip(".")
        port = parsed.port
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global and not fixture_only:
                return None
        except ValueError:
            if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
                if not fixture_only:
                    return None
            if not fixture_only:
                try:
                    addresses = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(
                        host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
                    )}
                except (OSError, ValueError):
                    return None
                if not addresses or any(not address.is_global for address in addresses):
                    return None
        if port not in (None, 80, 443) and not fixture_only:
            return None
        return parsed.scheme.casefold(), host, port
    except (TypeError, ValueError):
        return None


def _canonical_origin(origin):
    if origin is None:
        return None
    scheme, host, port = origin
    if port == (80 if scheme == "http" else 443):
        port = None
    return scheme, host, port


def bounded_get(client, url, expected_origin=None):
    """Fetch only through a transport that enforces its allowlist on redirects."""
    if not callable(getattr(client, "allowed", None)) or not getattr(client, "bounded_redirects", False):
        raise ValueError("COMPANY_DISCOVERY_CLIENT_UNSAFE")
    fixture_only = bool(getattr(client, "fixture_only", False))
    requested_origin = _origin(url, fixture_only)
    if expected_origin is not None and not isinstance(expected_origin, tuple):
        expected_origin = _origin(expected_origin, fixture_only)
    if (requested_origin is None or not client.allowed(url)
            or (expected_origin is not None and _canonical_origin(requested_origin) != _canonical_origin(expected_origin))):
        raise ValueError("COMPANY_DISCOVERY_URL_BLOCKED")
    response = client.get(url)
    final_url = str(getattr(response, "url", "") or "")
    final_origin = _origin(final_url, fixture_only)
    if (final_origin is None or not client.allowed(final_url)
            or _canonical_origin(final_origin) != _canonical_origin(requested_origin)):
        raise ValueError("COMPANY_DISCOVERY_REDIRECT_BLOCKED")
    return response
