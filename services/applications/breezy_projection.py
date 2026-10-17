"""Bounded, value-free static projection for Breezy application forms."""
from __future__ import annotations

import re

from services.applications.adapters.lever_markup import (
    MAX_MARKUP_LENGTH, analyze_lever_markup,
)


_SAFE_IDENTIFIER_ALIASES = {
    "address": "address", "city": "city", "country": "country", "email": "email",
    "email address": "email", "emailaddress": "email", "first name": "first_name",
    "firstname": "first_name", "given name": "first_name", "full name": "full_name",
    "fullname": "full_name", "last name": "last_name", "lastname": "last_name",
    "phone": "phone", "phone number": "phone", "phonenumber": "phone",
    "postal code": "postal_code", "postalcode": "postal_code", "state": "state",
    "website": "website", "zip code": "zip_code", "zipcode": "zip_code",
}
_SAFE_TYPES = {"text", "email", "tel", "url", "number", "date", "select", "textarea"}


def _report(reason: str) -> dict:
    return {"status": "REVIEW_REQUIRED", "reason": reason, "fields": []}


def _safe_identifier(value):
    if not isinstance(value, str) or not value.isascii():
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return _SAFE_IDENTIFIER_ALIASES.get(normalized)


def analyze_breezy_form_html(html, *, route_url) -> dict:
    """Project one public Breezy form into worker-safe static metadata.

    The shared Breezy route normalizer and existing bounded Lever parser provide
    route and markup validation. Static markup cannot establish rendered
    visibility, so all accepted forms remain subject to manual review.
    """
    if isinstance(html, bytes):
        if len(html) > MAX_MARKUP_LENGTH:
            return _report("MARKUP_LIMIT_EXCEEDED")
        try:
            source = html.decode("utf-8")
        except UnicodeDecodeError:
            return _report("INVALID_FORM_HTML")
    elif isinstance(html, str):
        try:
            encoded = html.encode("utf-8")
        except UnicodeEncodeError:
            return _report("INVALID_FORM_HTML")
        if len(encoded) > MAX_MARKUP_LENGTH:
            return _report("MARKUP_LIMIT_EXCEEDED")
        source = html
    else:
        return _report("INVALID_FORM_HTML")

    from services.applications.adapters.breezy import BreezyApplicationAdapter

    adapter = BreezyApplicationAdapter()
    normalized_route = adapter.normalize_route({"provider": "breezy", "url": route_url})
    if normalized_route is None:
        return _report("ROUTE_UNSUPPORTED")

    # The shared parser caps input size and control count. This cheap tag-count
    # guard also bounds parser work when compact markup contains many events.
    if source.count("<") > 4096:
        return _report("MARKUP_LIMIT_EXCEEDED")
    analysis = analyze_lever_markup(source)
    raw_fields = analysis.get("fields") if isinstance(analysis, dict) else None
    if not isinstance(raw_fields, list):
        return _report("ANALYSIS_FAILED")
    if analysis.get("status") != "PASS":
        reason = analysis.get("reason")
        if not isinstance(reason, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason):
            reason = "FORM_ANALYSIS_UNKNOWN"
        return _report(reason)

    fields = []
    for field in raw_fields:
        if not isinstance(field, dict):
            return _report("ANALYSIS_FAILED")
        safe_id = _safe_identifier(field.get("id"))
        safe_name = _safe_identifier(field.get("name"))
        raw_type = field.get("input_type") or field.get("tag")
        input_type = raw_type.casefold() if isinstance(raw_type, str) else ""
        field_type = input_type if input_type in _SAFE_TYPES else "other"

        if field.get("sensitivity") == "CREDENTIAL":
            review_reason = "CREDENTIAL_FIELD"
        elif field.get("sensitivity") == "SENSITIVE":
            review_reason = "SENSITIVE_FIELD"
        elif field.get("ambiguous") is True:
            review_reason = "AMBIGUOUS_FIELD"
        elif ((field.get("id") and safe_id is None)
              or (field.get("name") and safe_name is None)):
            review_reason = "UNSAFE_IDENTIFIER"
        elif not safe_id and not safe_name:
            review_reason = "UNIDENTIFIABLE_FIELD"
        elif field_type == "other":
            review_reason = "UNSUPPORTED_FIELD_TYPE"
        else:
            # Static HTML cannot establish that its label is actually visible.
            review_reason = "UNKNOWN_QUESTION"

        fields.append({
            "id": safe_id,
            "name": safe_name,
            "type": field_type,
            "required": field.get("required") is True,
            "review_reason": review_reason,
        })
    return {"status": "REVIEW_REQUIRED", "reason": "MANUAL_REVIEW_REQUIRED", "fields": fields}
