"""Review-first route and form metadata support for Recruitee postings.

This adapter recognizes tenant-hosted public job routes and delegates form
inspection to the shared value-free analyzer. It does not interact with forms,
upload documents, or submit applications.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter


_TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I)
_JOB_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?\Z", re.I)
_KNOWN_FIELDS = {
    "address", "city", "country", "cover letter", "cv", "email", "email address",
    "family name", "first name", "full name", "github", "github profile", "given name",
    "last name", "linkedin", "linkedin profile", "linkedin url", "location", "mobile",
    "mobile phone", "name", "personal website", "phone", "phone number", "portfolio",
    "postal code", "preferred name", "resume", "state", "surname", "website", "zip code",
}
_CHALLENGE = re.compile(r"\b(captcha|recaptcha|hcaptcha|turnstile|challenge|verification)\b", re.I)


def _normalized_metadata(value) -> str:
    """Normalize analyzer metadata without reading DOM control values."""
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip() if isinstance(value, str) else ""


class RecruiteeApplicationAdapter(GreenhouseApplicationAdapter):
    """Recognize Recruitee job routes and expose conservative form review."""

    provider = "recruitee"

    def capabilities(self) -> dict[str, bool]:
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
        """Use shared safe extraction and fail closed when challenge widgets exist."""
        analysis = super().analyze_page(page, check=check)
        if check is not None:
            check()
        selector = (
            'iframe[src*="captcha" i], iframe[src*="challenge" i], '
            '[id*="captcha" i], [class*="captcha" i], [name*="captcha" i], '
            '[id*="challenge" i], [class*="challenge" i], [data-sitekey]'
        )
        try:
            if page.locator(selector).count():
                return {"status": "UNKNOWN", "reason": "CHALLENGE_PRESENT", "fields": []}
        except Exception:
            if check is not None:
                check()
            return {"status": "UNKNOWN", "reason": "CHALLENGE_CHECK_UNAVAILABLE", "fields": []}
        if check is not None:
            check()
        return analysis

    def normalize_route(self, route: Mapping) -> dict | None:
        """Normalize https://<tenant>.recruitee.com/o/<job-slug> routes only."""
        if not isinstance(route, Mapping):
            return None
        provider, url = route.get("provider"), route.get("url")
        if not isinstance(provider, str) or provider.casefold() != self.provider:
            return None
        if not isinstance(url, str) or not url or len(url) > 2048:
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname.casefold() if parsed.hostname else ""
        suffix = ".recruitee.com"
        if (parsed.scheme.casefold() != "https" or not host.endswith(suffix)
                or parsed.username is not None or parsed.password is not None
                or port not in (None, 443) or parsed.fragment):
            return None
        tenant = host[:-len(suffix)]
        if not _TENANT.fullmatch(tenant):
            return None
        parts = parsed.path.split("/")
        if (len(parts) != 3 or parts[0] != "" or parts[1] != "o"
                or not _JOB_SLUG.fullmatch(parts[2])):
            return None
        slug = parts[2].casefold()
        supplied_tenant, supplied_requisition = route.get("tenant"), route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str) or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or supplied_requisition.casefold() != slug):
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": slug,
            "url": f"https://{tenant}.recruitee.com/o/{slug}",
        }

    def upload_mock_fixture_documents(self, page, analysis, snapshots, *, check=None):
        """Reject fixture upload because this provider adapter exposes no upload."""
        raise ValueError("RECRUITEE_DOCUMENT_UPLOAD_UNSUPPORTED")

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Require review for unknown, sensitive, ambiguous, custom or challenge fields."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list) or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if (not isinstance(field, Mapping) or field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"):
                return True
            metadata = [field.get(key) for key in
                        ("name", "id", "placeholder", "aria_label", "nearby_text",
                         "section_heading", "fieldset_legend", "labels", "options")]
            flattened = []
            for item in metadata:
                flattened.extend(item if isinstance(item, list) else [item])
            texts = [_normalized_metadata(value) for value in flattened]
            texts = [value for value in texts if value]
            if any(_CHALLENGE.search(value) for value in texts):
                return True
            labels = field.get("labels")
            prompts = [_normalized_metadata(value) for value in labels
                       if isinstance(value, str)] if isinstance(labels, list) else []
            prompts = [value for value in prompts if value]
            if prompts:
                if not all(value in _KNOWN_FIELDS for value in prompts):
                    return True
            elif field.get("tag") == "textarea" or not any(
                    value in _KNOWN_FIELDS for key in ("name", "id", "placeholder", "aria_label")
                    for value in [_normalized_metadata(field.get(key))] if value):
                return True
        return False
