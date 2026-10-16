"""Conservative metadata-only recognition for otherwise unsupported HTML forms.

This adapter deliberately does not fill controls, select documents, or submit
forms. A generic page has no provider contract, so every recognized field still
requires a person to review its meaning.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from services.applications.form_analysis import analyze_controls


_MAX_URL_LENGTH = 2048
_MAX_CONTROLS = 100


def _plain_host(host: str) -> bool:
    """Reject host spellings that need URL decoding or are otherwise ambiguous."""
    return bool(re.fullmatch(r"[A-Za-z0-9.-]+", host)) and ".." not in host


class GenericHTMLApplicationAdapter:
    """Normalize HTTPS routes and inspect bounded control metadata only."""

    provider = "generic_html"

    def capabilities(self) -> dict[str, bool]:
        return {
            "route_normalization": True,
            "bounded_control_recognition": True,
            "human_review_for_unknown_fields": True,
            "form_metadata_extraction": False,
            "autofill": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Canonicalize a syntactically safe HTTPS route without fetching it."""
        if not isinstance(route, Mapping) or route.get("provider") != self.provider:
            return None
        url = route.get("url")
        if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname
        if (parsed.scheme.casefold() != "https" or not host or not _plain_host(host)
                or parsed.username is not None or parsed.password is not None
                or port not in (None, 443) or parsed.fragment
                or any(ord(char) < 32 for char in url)):
            return None
        # Avoid path traversal and encoded separators that can change route meaning.
        if re.search(r"%(?:2f|5c|2e)", parsed.path, re.I):
            return None
        normalized = urlunsplit(("https", host.casefold(), parsed.path or "/", parsed.query, ""))
        supplied_host = route.get("host")
        if supplied_host is not None and (
                not isinstance(supplied_host, str) or supplied_host.casefold() != host.casefold()):
            return None
        return {"provider": self.provider, "tenant": host.casefold(), "url": normalized}

    def recognize_controls(self, controls) -> dict:
        """Analyze at most 100 value-free controls; never return values or answers."""
        if not isinstance(controls, list) or len(controls) > _MAX_CONTROLS:
            return {"status": "UNKNOWN", "reason": "CONTROL_LIMIT_EXCEEDED"
                    if isinstance(controls, list) else "INVALID_CONTROL_LIST", "fields": []}
        return analyze_controls(controls)

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Generic fields are never authorized for automatic interpretation."""
        # Every recognized field needs review: generic metadata has no provider
        # contract, even when the shared analyzer found it unambiguous. Unknown
        # and malformed analyses also fail closed.
        return True
