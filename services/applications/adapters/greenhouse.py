"""Review-first, read-only capability boundary for Greenhouse application routes.

This adapter identifies normalized public job routes and extracts only the
existing form analyzer's allowlisted DOM metadata. It does not interact with a
live form, upload files, or submit applications.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_form_page


_GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I)
_REQUISITION = re.compile(r"[0-9]{1,20}\Z")


class GreenhouseApplicationAdapter:
    """Recognize Greenhouse job routes and inspect forms without acting on them."""

    provider = "greenhouse"

    def capabilities(self) -> dict[str, bool]:
        """Return only behavior implemented and safe in this adapter slice."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize a public Greenhouse job URL; reject every other origin/shape."""
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
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() not in _GREENHOUSE_HOSTS
                or parsed.username is not None or parsed.password is not None
                or port not in (None, 443)):
            return None
        parts = parsed.path.split("/")
        if len(parts) != 4 or parts[0] != "" or parts[2] != "jobs":
            return None
        tenant, requisition = parts[1], parts[3]
        if not _TENANT.fullmatch(tenant) or not _REQUISITION.fullmatch(requisition):
            return None
        tenant = tenant.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and str(supplied_requisition) != requisition:
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://boards.greenhouse.io/{tenant}/jobs/{requisition}",
        }

    def analyze_page(self, page) -> dict:
        """Read one loaded form using the shared value-free DOM analyzer."""
        return analyze_form_page(page)

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Fail closed for unknown, malformed, ambiguous, or sensitive fields."""
        if not isinstance(analysis, Mapping) or analysis.get("status") != "PASS":
            return True
        fields = analysis.get("fields")
        if not isinstance(fields, list) or not fields:
            return True
        for field in fields:
            if not isinstance(field, Mapping):
                return True
            if field.get("ambiguous") is not False or field.get("sensitivity") != "STANDARD":
                return True
        return False
