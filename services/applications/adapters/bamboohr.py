"""Route-only recognition for public BambooHR careers job pages.

This adapter normalizes a strict public route shape. BambooHR form semantics,
document selection, live interaction, and submission are unsupported; supplied
forms always require human review and are never read or acted on.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit


_HOST = re.compile(
    r"(?P<tenant>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.bamboohr\.com\Z",
    re.I,
)
_JOB_ID = re.compile(r"[0-9]{1,20}\Z")
_ROUTE_QUERY_KEYS = {"id", "job", "jobid", "job_id", "posting", "requisition"}


class BambooHRApplicationAdapter:
    """Recognize BambooHR career-job routes without handling application forms."""

    provider = "bamboohr"

    def capabilities(self) -> dict[str, bool]:
        """Declare route recognition as the only supported provider capability."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": False,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize only https://<tenant>.bamboohr.com/careers/<numeric-id>."""
        if not isinstance(route, Mapping):
            return None
        provider = route.get("provider")
        url = route.get("url")
        if (not isinstance(provider, str) or provider.casefold() != self.provider
                or not isinstance(url, str) or not url or len(url) > 2048
                or url != url.strip() or "\\" in url
                or any(ord(char) < 32 for char in url)):
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https" or parsed.username is not None
                or parsed.password is not None or port is not None or parsed.fragment):
            return None

        host_match = _HOST.fullmatch(parsed.hostname or "")
        if host_match is None or host_match.group("tenant").casefold() == "www":
            return None
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0] != "" or parts[1] != "careers":
            return None
        requisition = parts[2]
        if not _JOB_ID.fullmatch(requisition):
            return None

        # Tracking parameters do not affect route identity. Reject malformed,
        # repeated, or route-bearing query keys instead of guessing precedence.
        try:
            query = parse_qsl(parsed.query, keep_blank_values=True,
                              strict_parsing=True, max_num_fields=32)
        except ValueError:
            return None
        keys = [key.casefold().replace("-", "_") for key, _ in query]
        if len(keys) != len(set(keys)) or any(
                key.replace("_", "") in _ROUTE_QUERY_KEYS for key in keys):
            return None

        tenant = host_match.group("tenant").casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or supplied_requisition != requisition):
            return None

        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://{tenant}.bamboohr.com/careers/{requisition}",
        }

    @staticmethod
    def analyze_form(_html) -> dict:
        """Fail closed because BambooHR form semantics are not supported."""
        return {
            "status": "REVIEW_REQUIRED",
            "reason": "FORM_SEMANTICS_UNSUPPORTED",
        }

    @staticmethod
    def requires_human_review(_analysis) -> bool:
        """Require human review for all forms until semantics are implemented."""
        return True
