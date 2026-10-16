"""Review-first capability boundary for public Ashby application routes."""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from services.applications.adapters.lever_markup import analyze_lever_markup


_ASHBY_HOST = "jobs.ashbyhq.com"
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")
_SAFE_IDENTIFIER = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.:\-\[\]]{0,99}\Z")
_PHONE_LIKE = re.compile(r"\+?\d[\d .()\-]{6,}\d\Z")
_SAFE_TYPES = {
    "text", "email", "tel", "url", "number", "date", "datetime-local", "time",
    "month", "week", "search", "password", "checkbox", "radio", "file", "select",
    "textarea",
}
_KNOWN_CONTACT_FIELDS = {
    "address", "city", "country", "cover letter", "cv", "email", "email address",
    "family name", "first name", "full name", "github", "github profile", "given name",
    "last name", "linkedin", "linkedin profile", "linkedin url", "location", "mobile",
    "mobile phone", "name", "personal website", "phone", "phone number", "phone type",
    "portfolio", "postal code", "preferred name", "resume", "state", "surname", "website",
    "zip code",
}
_SAFE_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def _safe_identifier(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not _SAFE_IDENTIFIER.fullmatch(normalized) or _PHONE_LIKE.fullmatch(normalized):
        return None
    return normalized


def _known_contact_field(field) -> bool:
    """Treat visible prompts as authoritative and custom questions as unknown."""
    if not isinstance(field, Mapping):
        return False
    labels = field.get("labels")
    if isinstance(labels, list) and labels:
        visible_labels = [
            label for label in labels if isinstance(label, str) and label.strip()
        ]
        if any(not label.isascii() for label in visible_labels):
            return False
        visible = [
            re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
            for label in visible_labels
        ]
        return bool(visible) and all(label in _KNOWN_CONTACT_FIELDS for label in visible)
    if field.get("tag") == "textarea":
        return False
    for key in ("name", "id", "placeholder", "aria_label"):
        values = field.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            for value in values:
                if (isinstance(value, str)
                        and re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
                        in _KNOWN_CONTACT_FIELDS):
                    return True
    return False


def _project_analysis(analysis) -> dict:
    """Return only allowlisted identifiers and fixed review reason codes."""
    if not isinstance(analysis, Mapping) or not isinstance(analysis.get("fields"), list):
        return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}

    fields = []
    for field in analysis["fields"]:
        if not isinstance(field, Mapping):
            return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
        field_id = _safe_identifier(field.get("id"))
        field_name = _safe_identifier(field.get("name"))
        input_type = field.get("input_type") or field.get("tag")
        field_type = input_type.casefold() if isinstance(input_type, str) else ""
        if field_type not in _SAFE_TYPES:
            field_type = "other"

        if field.get("sensitivity") == "CREDENTIAL":
            review_reason = "CREDENTIAL_FIELD"
        elif field.get("sensitivity") == "SENSITIVE":
            review_reason = "SENSITIVE_FIELD"
        elif field.get("ambiguous") is True:
            review_reason = "AMBIGUOUS_FIELD"
        elif ((field.get("id") and field_id is None)
              or (field.get("name") and field_name is None)):
            review_reason = "UNSAFE_IDENTIFIER"
        elif not field_id and not field_name:
            review_reason = "UNIDENTIFIABLE_FIELD"
        elif field_type == "other":
            review_reason = "UNSUPPORTED_FIELD_TYPE"
        elif not _known_contact_field(field):
            review_reason = "UNKNOWN_QUESTION"
        else:
            review_reason = None

        fields.append({
            "id": field_id,
            "name": field_name,
            "type": field_type,
            "required": field.get("required") is True,
            "review_reason": review_reason,
        })

    if analysis.get("status") != "PASS":
        reason = analysis.get("reason")
        if not isinstance(reason, str) or not _SAFE_REASON.fullmatch(reason):
            reason = "FORM_ANALYSIS_UNKNOWN"
        return {"status": "REVIEW_REQUIRED", "reason": reason, "fields": fields}
    if any(field["review_reason"] for field in fields):
        return {"status": "REVIEW_REQUIRED", "reason": "FORM_REQUIRES_HUMAN_REVIEW",
                "fields": fields}
    return {"status": "ANALYZED", "reason": None, "fields": fields}


class AshbyApplicationAdapter:
    """Normalize Ashby job routes and inspect static form markup without acting."""

    provider = "ashby"

    def capabilities(self) -> dict[str, bool]:
        """Declare the route and review behavior implemented by this adapter."""
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
        """Normalize only HTTPS Ashby job routes on the canonical public host."""
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
        host = parsed.hostname.casefold() if parsed.hostname else ""
        if (parsed.scheme.casefold() != "https" or host != _ASHBY_HOST
                or parsed.username is not None or parsed.password is not None
                or port is not None or parsed.fragment):
            return None

        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0] != "":
            return None
        tenant, requisition = parts[1], parts[2]
        if not _IDENTIFIER.fullmatch(tenant) or not _IDENTIFIER.fullmatch(requisition):
            return None
        tenant, requisition = tenant.casefold(), requisition.casefold()
        supplied_tenant = route.get("tenant")
        supplied_requisition = route.get("requisition")
        if (supplied_tenant is not None and
                (not isinstance(supplied_tenant, str)
                 or not _IDENTIFIER.fullmatch(supplied_tenant)
                 or supplied_tenant.casefold() != tenant)):
            return None
        if (supplied_requisition is not None and
                (not isinstance(supplied_requisition, str)
                 or not _IDENTIFIER.fullmatch(supplied_requisition)
                 or supplied_requisition.casefold() != requisition)):
            return None
        return {
            "provider": self.provider,
            "tenant": tenant,
            "requisition": requisition,
            "url": f"https://{_ASHBY_HOST}/{tenant}/{requisition}",
        }

    def analyze_form(self, markup, *, route_url) -> dict:
        """Analyze supplied Ashby markup locally and return value-free metadata only."""
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
            return _project_analysis(analyze_lever_markup(markup))
        except Exception:
            # Parser exception text may contain supplied private form data.
            return {"status": "REVIEW_REQUIRED", "reason": "ANALYSIS_FAILED", "fields": []}
