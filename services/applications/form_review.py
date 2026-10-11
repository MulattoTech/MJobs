"""Bounded, value-free analysis of fetched Greenhouse form HTML."""
from __future__ import annotations

from html.parser import HTMLParser
import re

from services.applications.adapters.greenhouse import GreenhouseApplicationAdapter
from services.applications.form_analysis import (
    MAX_CONTROLS,
    MAX_METADATA_LENGTH,
    MAX_OPTIONS,
    analyze_controls,
)


MAX_FORM_HTML_BYTES = 2 * 1024 * 1024
_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "challenge", "cloudflare",
    "cf-chl-", "datadome", "perimeterx", "arkose", "funcaptcha", "geetest", "bot-check",
)
_SAFE_IDENTIFIER = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.:\-\[\]]{0,99}\Z")
_SAFE_TYPES = {
    "text", "email", "tel", "url", "number", "date", "datetime-local", "time",
    "month", "week", "search", "password", "checkbox", "radio", "file", "select", "textarea",
}


class _FormMetadataParser(HTMLParser):
    """Collect analyzer inputs while discarding control values and page text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.form_count = 0
        self.controls = []
        self.challenge_protected = False
        self.invalid = False
        self._active_form = False
        self._labels = []
        self._label_text = {}
        self._option = None
        self._select = None

    def _challenge(self, value):
        text = str(value or "").casefold()
        if any(marker in text for marker in _CHALLENGE_MARKERS):
            self.challenge_protected = True

    @staticmethod
    def _attrs(attrs):
        names = [str(name).casefold() for name, _ in attrs]
        if len(names) != len(set(names)):
            return None
        return {str(name).casefold(): str(value or "") for name, value in attrs}

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        values = self._attrs(attrs)
        if values is None:
            self.invalid = True
            return
        self._challenge(" ".join((*values.keys(), *values.values())))
        if tag == "form":
            self.form_count += 1
            self._active_form = self.form_count == 1
            return
        if tag == "label":
            self._labels.append({"for": values.get("for"), "parts": []})
            return
        if not self._active_form:
            return
        if tag in {"input", "select", "textarea"}:
            input_type = values.get("type", "text").casefold() if tag == "input" else tag
            if tag == "input" and input_type in {"hidden", "submit", "button", "reset", "image"}:
                return
            if len(self.controls) >= MAX_CONTROLS + 1:
                self.invalid = True
                return
            self.controls.append({
                "tag": tag,
                "labels": [],
                "name": values.get("name"),
                "id": values.get("id"),
                "placeholder": values.get("placeholder"),
                "aria_label": values.get("aria-label"),
                "input_type": input_type,
                "required": "required" in values or values.get("aria-required", "").casefold() == "true",
            })
            if tag == "select":
                self._select = self.controls[-1]
            return
        if tag == "option" and self._select is not None:
            self._option = {"parts": []}

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        self._challenge(data)
        for label in self._labels:
            if sum(map(len, label["parts"])) <= MAX_METADATA_LENGTH:
                label["parts"].append(data)
            else:
                self.invalid = True
        if self._option is not None:
            if sum(map(len, self._option["parts"])) <= MAX_METADATA_LENGTH:
                self._option["parts"].append(data)
            else:
                self.invalid = True

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag == "label" and self._labels:
            label = self._labels.pop()
            text = " ".join(" ".join(label["parts"]).split())
            if len(text) > MAX_METADATA_LENGTH:
                self.invalid = True
            elif text:
                self._label_text.setdefault(label["for"], []).append(text)
                if label["for"] is None and self.controls:
                    self.controls[-1]["labels"].append(text)
            return
        if tag == "option" and self._option is not None:
            text = " ".join(" ".join(self._option["parts"]).split())
            if text and self._select is not None:
                options = self._select.setdefault("options", [])
                if len(options) >= MAX_OPTIONS + 1:
                    self.invalid = True
                else:
                    options.append(text)
            self._option = None
            return
        if tag == "select":
            self._select = None
            return
        if tag == "form":
            self._active_form = False

    def finish(self):
        if self._labels or self._option is not None or self.invalid:
            return {"status": "UNKNOWN", "reason": "INVALID_FORM_METADATA", "fields": []}
        if self.form_count == 0:
            return {"status": "UNKNOWN", "reason": "NO_FORM", "fields": []}
        if self.form_count != 1:
            return {"status": "UNKNOWN", "reason": "AMBIGUOUS_FORM", "fields": []}
        for control in self.controls:
            field_id = control.get("id")
            if field_id:
                control["labels"].extend(self._label_text.get(field_id, []))
        required = [control.get("required") is True for control in self.controls]
        controls = [{key: value for key, value in control.items() if key != "required"}
                    for control in self.controls]
        analysis = analyze_controls(controls)
        for field in analysis.get("fields", []):
            index = field.get("index")
            if isinstance(index, int) and 0 <= index < len(required):
                field["required"] = required[index]
        return analysis


def _safe_identifier(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        return None
    if re.fullmatch(r"\+?\d[\d .()\-]{6,}\d", normalized):
        return None
    return normalized


def _sanitized_fields(analysis):
    fields = []
    for field in analysis.get("fields", []):
        sensitivity = field.get("sensitivity")
        ambiguous = field.get("ambiguous") is True
        identifier_invalid = ((field.get("id") and _safe_identifier(field.get("id")) is None)
                              or (field.get("name") and _safe_identifier(field.get("name")) is None))
        input_type = field.get("input_type") or field.get("tag")
        normalized_type = input_type.casefold() if isinstance(input_type, str) else ""
        if normalized_type not in _SAFE_TYPES:
            normalized_type = "other"
        if sensitivity == "CREDENTIAL":
            review_reason = "CREDENTIAL_FIELD"
        elif sensitivity == "SENSITIVE":
            review_reason = "SENSITIVE_FIELD"
        elif ambiguous:
            review_reason = "AMBIGUOUS_FIELD"
        elif identifier_invalid:
            review_reason = "UNSAFE_IDENTIFIER"
        elif not field.get("id") and not field.get("name"):
            review_reason = "UNIDENTIFIABLE_FIELD"
        elif normalized_type == "other":
            review_reason = "UNSUPPORTED_FIELD_TYPE"
        else:
            review_reason = None
        fields.append({
            "id": _safe_identifier(field.get("id")),
            "name": _safe_identifier(field.get("name")),
            "type": normalized_type,
            "required": field.get("required") is True,
            "review_reason": review_reason,
        })
    return fields


def analyze_form_html(provider, html, *, route_url):
    """Analyze fetched Greenhouse HTML into a bounded, value-free report.

    Fetching is intentionally outside this pure function; callers must use the
    repository's bounded public client before passing untrusted response bytes.
    """
    adapter = GreenhouseApplicationAdapter()
    normalized = adapter.normalize_route({"provider": provider, "url": route_url})
    if normalized is None:
        return {"status": "REVIEW_REQUIRED", "reason": "ROUTE_UNSUPPORTED", "fields": []}
    if isinstance(html, bytes):
        raw = html
    elif isinstance(html, str):
        if len(html) > MAX_FORM_HTML_BYTES:
            return {"status": "REVIEW_REQUIRED", "reason": "HTML_TOO_LARGE", "fields": []}
        try:
            raw = html.encode("utf-8")
        except UnicodeEncodeError:
            return {"status": "REVIEW_REQUIRED", "reason": "INVALID_FORM_HTML", "fields": []}
    else:
        return {"status": "REVIEW_REQUIRED", "reason": "INVALID_FORM_HTML", "fields": []}
    if len(raw) > MAX_FORM_HTML_BYTES:
        return {"status": "REVIEW_REQUIRED", "reason": "HTML_TOO_LARGE", "fields": []}

    parser = _FormMetadataParser()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:
        return {"status": "REVIEW_REQUIRED", "reason": "INVALID_FORM_HTML", "fields": []}
    if parser.challenge_protected:
        return {"status": "REVIEW_REQUIRED", "reason": "CHALLENGE_PROTECTED", "fields": []}

    analysis = parser.finish()
    fields = _sanitized_fields(analysis)
    if analysis.get("status") != "PASS":
        return {"status": "REVIEW_REQUIRED", "reason": analysis.get("reason") or "FORM_ANALYSIS_UNKNOWN",
                "fields": fields}
    if any(field["review_reason"] for field in fields):
        return {"status": "REVIEW_REQUIRED", "reason": "FORM_REQUIRES_HUMAN_REVIEW", "fields": fields}
    return {"status": "ANALYZED", "reason": None, "fields": fields}
