"""Review-first route and local form metadata support for iCIMS."""
from __future__ import annotations

from collections.abc import Mapping
import re
from urllib.parse import urlsplit

from services.applications.adapters.lever_markup import analyze_lever_markup
from services.applications.form_review_providers import _contact_provider_report


_HOST = re.compile(r"careers-([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.icims\.com\Z")
_REQUISITION = re.compile(r"[0-9]{1,32}\Z")
_JOB_SLUG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,158}[A-Za-z0-9])?\Z")
_TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_KNOWN_FIELDS = frozenset({
    "address", "city", "country", "cover letter", "cv", "email", "email address",
    "family name", "first name", "full name", "github", "github profile", "given name",
    "last name", "linkedin", "linkedin profile", "linkedin url", "location", "mobile",
    "mobile phone", "name", "personal website", "phone", "phone number", "portfolio",
    "postal code", "preferred name", "resume", "state", "surname", "website", "zip code",
})


def _normalized_prompt(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


class ICIMSApplicationAdapter:
    """Recognize canonical iCIMS job routes and inspect supplied markup only."""

    provider = "icims"

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

    def normalize_route(self, route: Mapping) -> dict | None:
        """Accept only HTTPS careers-<tenant>.icims.com job identity routes."""
        if not isinstance(route, Mapping):
            return None
        provider, url = route.get("provider"), route.get("url")
        if (not isinstance(provider, str) or not provider.isascii()
                or provider.casefold() != self.provider
                or not isinstance(url, str) or not url or len(url) > 2048
                or not url.isascii() or url != url.strip() or "\\" in url
                or any(ord(char) < 32 for char in url)):
            return None
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname.casefold() if parsed.hostname else ""
        match = _HOST.fullmatch(host)
        if (parsed.scheme.casefold() != "https" or match is None
                or parsed.username is not None or parsed.password is not None
                or port is not None or parsed.fragment):
            return None
        tenant = match.group(1)
        parts = parsed.path.split("/")
        if (len(parts) != 5 or parts[0] != "" or parts[1] != "jobs"
                or not _REQUISITION.fullmatch(parts[2])
                or not _JOB_SLUG.fullmatch(parts[3]) or parts[4] != "job"):
            return None
        requisition = parts[2]
        job_slug = parts[3]
        supplied_tenant, supplied_requisition = route.get("tenant"), route.get("requisition")
        if supplied_tenant is not None and (
                not isinstance(supplied_tenant, str) or not supplied_tenant.isascii()
                or not _TENANT.fullmatch(supplied_tenant)
                or supplied_tenant.casefold() != tenant):
            return None
        if supplied_requisition is not None and (
                not isinstance(supplied_requisition, str)
                or not _REQUISITION.fullmatch(supplied_requisition)
                or supplied_requisition != requisition):
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://careers-{tenant}.icims.com/jobs/{requisition}/{job_slug}/job",
        }

    @staticmethod
    def requires_human_review(analysis) -> bool:
        """Treat custom, ambiguous, sensitive, and unrecognized prompts as review."""
        if (not isinstance(analysis, Mapping) or analysis.get("status") != "PASS"
                or not isinstance(analysis.get("fields"), list)
                or not analysis["fields"]):
            return True
        for field in analysis["fields"]:
            if (not isinstance(field, Mapping) or field.get("ambiguous") is not False
                    or field.get("sensitivity") != "STANDARD"):
                return True
            labels = field.get("labels")
            if isinstance(labels, list) and labels:
                visible = [label for label in labels if isinstance(label, str) and label.strip()]
                if not visible or any(not label.isascii()
                                      or _normalized_prompt(label) not in _KNOWN_FIELDS
                                      for label in visible):
                    return True
            elif (field.get("tag") == "textarea" or not any(
                    isinstance(field.get(key), str)
                    and field.get(key).isascii()
                    and _normalized_prompt(field[key]) in _KNOWN_FIELDS
                    for key in ("name", "id", "placeholder", "aria_label"))):
                return True
        return False

    def analyze_form(self, markup, *, route_url) -> dict:
        """Project bounded supplied HTML to value-free metadata; never fetch or act."""
        if self.normalize_route({"provider": self.provider, "url": route_url}) is None:
            return {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []}
        if isinstance(markup, bytes):
            if len(markup) > 100_000:
                return {"status": "REVIEW_REQUIRED", "reason": "HTML_TOO_LARGE", "fields": []}
            try:
                markup = markup.decode("utf-8")
            except UnicodeDecodeError:
                return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
        elif isinstance(markup, str):
            if len(markup.encode("utf-8", errors="replace")) > 100_000:
                return {"status": "REVIEW_REQUIRED", "reason": "HTML_TOO_LARGE", "fields": []}
        else:
            return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
        try:
            analysis = analyze_lever_markup(markup)
            report = _contact_provider_report(analysis, type(self))
        except Exception:
            return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
        if any(field.get("required") for field in report["fields"]):
            for field in report["fields"]:
                if field["required"] and field["review_reason"] is None:
                    field["review_reason"] = "REQUIRED_FIELD_REVIEW"
            report["status"] = "REVIEW_REQUIRED"
            report["reason"] = "FORM_REQUIRES_HUMAN_REVIEW"
        return report
