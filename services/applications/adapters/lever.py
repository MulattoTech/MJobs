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
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I | re.ASCII)
_REQUISITION = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.I,
)
_KNOWN_CONTACT_FIELDS = {
    "address", "city", "country", "cover letter", "cv", "email", "email address",
    "family name", "first name", "full name", "github", "github profile", "given name",
    "last name", "linkedin", "linkedin profile", "linkedin url", "location", "mobile",
    "mobile phone", "name", "personal website", "phone", "phone number", "phone type",
    "portfolio", "postal code", "preferred name", "resume", "state", "surname", "website",
    "zip code",
}


def _known_contact_field(field) -> bool:
    """Accept only recognizable contact/document prompts for unattended review."""
    if not isinstance(field, Mapping):
        return False
    labels = field.get("labels")
    visible_labels = []
    if isinstance(labels, list):
        if any(isinstance(label, str) and not label.isascii() for label in labels):
            return False
        visible_labels = [
            re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
            for label in labels if isinstance(label, str) and label.strip()
        ]
    if visible_labels:
        # A visible prompt is authoritative; familiar ids cannot override it.
        return all(label in _KNOWN_CONTACT_FIELDS for label in visible_labels)
    tag = field.get("tag")
    if isinstance(tag, str) and tag.casefold() == "textarea":
        # Free-form fields are custom questions even when their id looks familiar.
        return False
    for key in ("placeholder", "aria_label"):
        prompts = field.get(key)
        if isinstance(prompts, str):
            prompts = [prompts]
        if isinstance(prompts, list) and any(
                isinstance(prompt, str) and not prompt.isascii() for prompt in prompts):
            return False
    for key in ("name", "id", "placeholder", "aria_label"):
        values = field.get(key)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        if any(isinstance(value, str) and not value.isascii() for value in values):
            return False
        for value in values:
            if isinstance(value, str):
                normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
                if normalized in _KNOWN_CONTACT_FIELDS:
                    return True
    return False


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
        if (not isinstance(url, str) or not url or len(url) > 2048
                or url != url.strip() or "\\" in url
                or any(ord(char) < 32 for char in url)):
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
        # Lever's global and EU hosts identify separate instances. Preserve
        # the allowlisted origin in review fingerprints and worker fetches.
        host = parsed.hostname.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        regional_tenant = ('eu' if host == 'jobs.eu.lever.co' else 'global') + ':' + tenant
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str)
                or not supplied_tenant.isascii()
                or supplied_tenant.casefold() not in {tenant, regional_tenant}):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or supplied_requisition.casefold() != requisition):
            return None
        return {
            "provider": self.provider,
            "tenant": regional_tenant if supplied_tenant and ':' in supplied_tenant else tenant,
            "requisition": requisition,
            "url": f"https://{host}/{tenant}/{requisition}",
        }

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require review for custom prompts as well as sensitive or ambiguous fields."""
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
        return False
