"""Review-first route and form metadata boundary for Workable applications.

This adapter recognizes public Workable job routes and reuses the shared
value-free form analyzer. Unknown prompts and challenge-protected controls
require manual review. It has no live form action or submission path.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.adapters.lever import _known_contact_field
from services.applications.form_analysis import analyze_form_page


_WORKABLE_HOST = "apply.workable.com"
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I)
_REQUISITION = re.compile(r"[a-z0-9][a-z0-9_-]{4,39}\Z", re.I)
_CHALLENGE_MARKERS = re.compile(
    r"\b(captcha|recaptcha|hcaptcha|turnstile|challenge|cloudflare|bot verification|human verification)\b",
    re.I,
)
_CHALLENGE_SELECTOR = (
    'iframe[src*="captcha" i], iframe[title*="captcha" i], '
    '[class*="captcha" i], [id*="captcha" i], '
    'iframe[src*="turnstile" i], iframe[src*="challenges.cloudflare.com" i], '
    '[class*="turnstile" i], [id*="turnstile" i], '
    '[class*="cf-chl-" i], [id*="cf-chl-" i], [data-sitekey]'
)


class WorkableApplicationAdapter:
    """Recognize Workable job routes and inspect forms without acting on them."""

    provider = "workable"

    def capabilities(self) -> dict[str, bool]:
        """Declare only route and review behavior implemented in this slice."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Analyze value-free controls and detect common embedded challenge widgets."""
        if check is not None:
            analysis = analyze_form_page(page, check=check)
        else:
            analysis = analyze_form_page(page)
        if analysis.get("status") != "PASS":
            return analysis
        if check is not None:
            check()
        try:
            challenge_count = page.locator(_CHALLENGE_SELECTOR).count()
        except Exception:
            if check is not None:
                check()
            return {"status": "UNKNOWN", "reason": "CHALLENGE_CHECK_UNAVAILABLE", "fields": []}
        if check is not None:
            check()
        if challenge_count:
            return {"status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED_FORM", "fields": []}
        return analysis

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize an allowlisted Workable posting URL or reject it."""
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
                or (parsed.hostname or "").casefold() != _WORKABLE_HOST
                or parsed.username is not None or parsed.password is not None
                or port is not None or parsed.fragment):
            return None

        parts = parsed.path.split("/")
        if len(parts) == 5 and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) != 4 or parts[0] != "" or parts[2] != "j":
            return None
        tenant, requisition = parts[1], parts[3]
        if not _TENANT.fullmatch(tenant) or not _REQUISITION.fullmatch(requisition):
            return None
        tenant, requisition = tenant.casefold(), requisition.casefold()
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
            "url": f"https://{_WORKABLE_HOST}/{tenant}/j/{requisition}",
        }

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require review for uncertainty, sensitive data, challenges, or custom prompts."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if (not isinstance(field, Mapping)
                    or field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"
                    or not _known_contact_field(field)):
                return True
            metadata = " ".join(
                value for key, value in field.items()
                if key in {"name", "id", "placeholder", "aria_label", "label"}
                and isinstance(value, str)
            )
            labels = field.get("labels")
            if isinstance(labels, list):
                metadata += " " + " ".join(value for value in labels if isinstance(value, str))
            normalized_metadata = re.sub(r"[^a-z0-9]+", " ", metadata.casefold())
            if _CHALLENGE_MARKERS.search(normalized_metadata):
                return True
        return False
