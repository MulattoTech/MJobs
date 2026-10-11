"""Review-first capability boundary for public Lever application routes.

Lever shares the existing value-free form analyzer and review gates with the
Greenhouse adapter. Route recognition is provider-specific; live form actions
and public document uploads remain disabled.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter


_LEVER_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I)
_REQUISITION = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.I,
)


class LeverApplicationAdapter(GreenhouseApplicationAdapter):
    """Recognize Lever job routes and reuse safe, review-first form inspection."""

    provider = "lever"

    def capabilities(self) -> dict[str, bool]:
        """Return only behavior implemented and safe in this adapter slice."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize an allowlisted Lever job URL; reject all other URL shapes."""
        if not isinstance(route, Mapping):
            return None
        provider = route.get("provider")
        url = route.get("url")
        if not isinstance(provider, str) or provider.casefold() != self.provider:
            return None
        if not isinstance(url, str) or not url or len(url) > 2048:
            return None
        try:
            parsed = urlsplit(url)
            # Accessing .port also rejects malformed port values. Lever routes
            # accept no explicit port, including an explicit default HTTPS port.
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() not in _LEVER_HOSTS
                or parsed.username is not None or parsed.password is not None
                or port is not None or parsed.fragment):
            return None

        parts = parsed.path.split("/")
        if len(parts) not in (3, 4) or parts[0] != "" or (len(parts) == 4 and parts[3] != "apply"):
            return None
        tenant, requisition = parts[1], parts[2]
        if not _TENANT.fullmatch(tenant) or not _REQUISITION.fullmatch(requisition):
            return None
        tenant = tenant.casefold()
        requisition = requisition.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or supplied_requisition.casefold() != requisition):
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://jobs.lever.co/{tenant}/{requisition}",
        }
