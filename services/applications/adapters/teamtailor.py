"""Review-first support for canonical Teamtailor career-site routes.

This adapter normalizes public job URLs and inspects an already-loaded page for
value-free form metadata. It never fills controls, uploads documents, or submits
applications. Unsupported or challenge-protected forms require manual review.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_form_page


_TEAMTAILOR_HOST = re.compile(r"(?P<tenant>[a-z0-9][a-z0-9-]{0,62})\.teamtailor\.com\Z", re.I)
_JOB_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,119}\Z", re.I)
_CHALLENGE_SELECTORS = (
    '[class*="captcha" i]', '[id*="captcha" i]', 'iframe[src*="captcha" i]',
    '[class*="turnstile" i]', '[id*="turnstile" i]', '[data-sitekey]',
    '[data-cf-chl]', 'iframe[src*="challenge" i]',
)
_KNOWN_CONTACT_IDENTIFIERS = {
    "address", "city", "country", "cover letter", "cv", "email",
    "email address", "family name", "first name", "full name", "github",
    "github profile", "given name", "last name", "linkedin",
    "linkedin profile", "linkedin url", "location", "mobile",
    "mobile phone", "name", "personal website", "phone", "phone number",
    "portfolio", "postal code", "preferred name", "resume", "state",
    "surname", "website", "zip code",
}
_USER_EDITABLE_INPUT_TYPES = {
    "text", "email", "tel", "url", "number", "date", "datetime-local",
    "time", "month", "week", "search", "checkbox", "radio",
}


class TeamtailorApplicationAdapter:
    """Recognize canonical Teamtailor job routes and inspect forms safely."""

    provider = "teamtailor"

    def capabilities(self) -> dict[str, bool]:
        """List only the route and value-free review behavior implemented here."""
        return {
            "route_normalization": True,
            "form_metadata_extraction": True,
            "human_review_for_uncertainty": True,
            "document_upload": False,
            "live_form_interaction": False,
            "submission": False,
        }

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize only canonical tenant.teamtailor.com/jobs/{slug} URLs."""
        if not isinstance(route, Mapping):
            return None
        provider = route.get("provider")
        url = route.get("url")
        if (not isinstance(provider, str) or provider.casefold() != self.provider
                or not isinstance(url, str) or not url or len(url) > 2048):
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        host = (parsed.hostname or "").casefold()
        match = _TEAMTAILOR_HOST.fullmatch(host)
        if (parsed.scheme.casefold() != "https" or match is None or port is not None
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            return None
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0] != "" or parts[1] != "jobs":
            return None
        slug = parts[2]
        if not _JOB_SLUG.fullmatch(slug):
            return None

        tenant = match.group("tenant").casefold()
        slug = slug.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if (supplied_tenant is not None
                and (not isinstance(supplied_tenant, str)
                     or supplied_tenant.casefold() != tenant)):
            return None
        if (supplied_requisition is not None
                and (not isinstance(supplied_requisition, str)
                     or supplied_requisition.casefold() != slug)):
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": slug,
            "url": f"https://{tenant}.teamtailor.com/jobs/{slug}",
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Inspect a loaded form's safe metadata; challenge signals stop analysis."""
        if check is not None:
            check()
        try:
            for selector in _CHALLENGE_SELECTORS:
                if check is not None:
                    check()
                if page.locator(selector).count() > 0:
                    return {"status": "UNKNOWN", "reason": "CHALLENGE_PROTECTED", "fields": []}
        except Exception:
            if check is not None:
                check()
            return {"status": "UNKNOWN", "reason": "CHALLENGE_DETECTION_UNAVAILABLE", "fields": []}
        return analyze_form_page(page, check=check) if check is not None else analyze_form_page(page)

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require manual review unless every field is a known standard contact field."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if (not isinstance(field, Mapping) or field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"
                    or not _known_contact_field(field)):
                return True
        return False


def _known_contact_field(field: Mapping) -> bool:
    """Recognize a narrow set of contact/document prompts, never custom questions."""
    tag = field.get("tag")
    if tag == "input":
        input_type = field.get("input_type")
        if (not isinstance(input_type, str)
                or input_type.casefold() not in _USER_EDITABLE_INPUT_TYPES):
            return False
    elif tag == "select":
        if field.get("input_type") is not None:
            return False
    else:
        # Free-form text areas and unknown controls always need a person.
        return False

    labels = field.get("labels")
    visible_labels = []
    if isinstance(labels, list):
        visible_labels = [
            re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
            for label in labels if isinstance(label, str) and label.strip()
        ]
    if visible_labels:
        return all(label in _KNOWN_CONTACT_IDENTIFIERS for label in visible_labels)
    for key in ("name", "id", "placeholder", "aria_label"):
        values = field.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
                    if normalized in _KNOWN_CONTACT_IDENTIFIERS:
                        return True
    return False
