"""Review-first route and form metadata support for Breezy career pages.

Only public tenant job routes are recognized. Form inspection uses the shared
value-free analyzer; uploads, live interaction, and submission are unsupported.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_form_page


_TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I)
_POSITION = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\Z", re.I,
)
_CONTACT_PROMPTS = {
    "address", "city", "country", "email", "email address", "first name",
    "full name", "last name", "location", "mobile", "name", "phone",
    "phone number", "postal code", "state", "website", "zip code",
}
_SUPPORTED_INPUT_TYPES = {"text", "email", "tel"}


class BreezyApplicationAdapter:
    """Recognize strict Breezy job links and inspect forms without acting on them."""

    provider = "breezy"

    def capabilities(self) -> dict[str, bool]:
        """Declare the narrow inspection-only boundary implemented here."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "mock_fixture_upload": False,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize tenant.breezy.hr job routes, rejecting ambiguous shapes."""
        if not isinstance(route, Mapping):
            return None
        provider, url = route.get("provider"), route.get("url")
        if not isinstance(provider, str) or provider.casefold() != self.provider:
            return None
        if not isinstance(url, str) or not url or len(url) > 2048 or "\\" in url:
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme.casefold() != "https" or port is not None
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            return None
        host = (parsed.hostname or "").casefold()
        suffix = ".breezy.hr"
        if not host.endswith(suffix):
            return None
        tenant = host[:-len(suffix)]
        if not _TENANT.fullmatch(tenant):
            return None
        parts = parsed.path.split("/")
        if len(parts) not in (3, 4) or parts[0] != "" or parts[1] != "p":
            return None
        if len(parts) == 4 and parts[3] != "apply":
            return None
        position = _POSITION.fullmatch(parts[2])
        if position is None:
            return None
        requisition = position.group("id").casefold()
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
        slug = position.group("slug").casefold()
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://{tenant}.breezy.hr/p/{requisition}-{slug}",
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Read form metadata only; no controls are filled, clicked, or submitted."""
        if check is not None:
            return analyze_form_page(page, check=check)
        return analyze_form_page(page)

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require review for unknown, sensitive, custom, or unsupported fields."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if not isinstance(field, Mapping):
                return True
            input_type = field.get("input_type") or "text"
            if (field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"
                    or not isinstance(input_type, str)
                    or input_type.casefold() not in _SUPPORTED_INPUT_TYPES
                    or not BreezyApplicationAdapter._known_contact_prompt(field)):
                return True
        return False

    @staticmethod
    def _known_contact_prompt(field: Mapping) -> bool:
        labels = field.get("labels")
        if isinstance(labels, list) and labels:
            normalized = [re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
                          for label in labels if isinstance(label, str)]
            return bool(normalized) and all(label in _CONTACT_PROMPTS for label in normalized)
        for key in ("name", "id", "placeholder", "aria_label"):
            value = field.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str):
                    normalized = re.sub(r"[^a-z0-9]+", " ", item.casefold()).strip()
                    if normalized in _CONTACT_PROMPTS:
                        return True
        return False
