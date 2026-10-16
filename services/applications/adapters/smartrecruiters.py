"""Review-first route and form metadata boundary for SmartRecruiters.

Only canonical public job routes are recognized. Form inspection returns the
shared value-free metadata contract; uncertain forms and challenge-protected
pages stop for manual review. This adapter has no upload or submission path.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_form_page


_HOST = "jobs.smartrecruiters.com"
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z", re.I)
_JOB_PATH = re.compile(r"([0-9]{8,20})(?:-([a-z0-9][a-z0-9_-]{0,159}))?\Z", re.I)
_CHALLENGE_SELECTOR = (
    'iframe[src*="captcha" i], iframe[src*="challenges.cloudflare.com" i], '
    'iframe[src*="arkoselabs.com" i], iframe[src*="funcaptcha" i], '
    '[class*="captcha" i], [id*="captcha" i], [name*="captcha" i], [data-sitekey], '
    '[class*="hcaptcha" i], [id*="hcaptcha" i], '
    '[class*="turnstile" i], [id*="turnstile" i], '
    '[data-datadome], [class*="datadome" i], [id*="datadome" i], '
    '[id*="geetest" i], [class*="geetest" i], '
    '[id*="arkose-container" i], [class*="arkose-container" i], '
    '[id*="cf-chl-" i], [class*="cf-chl-" i], [data-cf-chl]'
)


class SmartRecruitersApplicationAdapter:
    """Recognize SmartRecruiters job routes and inspect forms without acting."""

    provider = "smartrecruiters"

    def capabilities(self) -> dict[str, bool]:
        """Declare only route recognition and value-free review support."""
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
        """Normalize a canonical HTTPS SmartRecruiters job URL or reject it."""
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
        if (parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() != _HOST
                or parsed.username is not None or parsed.password is not None
                or port not in (None, 443) or parsed.fragment):
            return None
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0] != "":
            return None
        tenant, job_path = parts[1], parts[2]
        match = _JOB_PATH.fullmatch(job_path)
        if not _TENANT.fullmatch(tenant) or match is None:
            return None
        tenant = tenant.casefold()
        requisition, slug = match.groups()
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
        canonical_path = requisition + (f"-{slug}" if slug else "")
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://{_HOST}/{tenant}/{canonical_path}",
        }

    def analyze_page(self, page, *, check=None) -> dict:
        """Inspect one allowlisted-origin page without reading control values."""
        if check is not None:
            check()
        try:
            parsed = urlsplit(page.url)
            port = parsed.port
        except (AttributeError, TypeError, ValueError):
            return self._unknown("UNTRUSTED_FORM_ORIGIN")
        if (parsed.scheme.casefold() != "https"
                or (parsed.hostname or "").casefold() != _HOST
                or parsed.username is not None or parsed.password is not None
                or port not in (None, 443)):
            return self._unknown("UNTRUSTED_FORM_ORIGIN")
        try:
            challenge_present = page.locator(_CHALLENGE_SELECTOR).count() > 0
        except Exception:
            if check is not None:
                check()
            return self._unknown("CHALLENGE_CHECK_UNAVAILABLE")
        if check is not None:
            check()
        if challenge_present:
            return self._unknown("CHALLENGE_PROTECTED_FORM")
        if check is not None:
            return analyze_form_page(page, check=check)
        return analyze_form_page(page)

    @staticmethod
    def _unknown(reason: str) -> dict:
        return {"status": "UNKNOWN", "reason": reason, "fields": []}

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Fail closed for malformed, uncertain, ambiguous, or sensitive fields."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        return any(
            not isinstance(field, Mapping)
            or field.get("ambiguous") is not False
            or field.get("sensitivity") != "STANDARD"
            for field in analysis["fields"]
        )
