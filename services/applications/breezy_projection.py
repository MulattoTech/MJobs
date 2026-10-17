"""Bounded, value-free static projection for Breezy application forms.

Static markup cannot prove what an applicant can see. Every successful
projection therefore remains review-required; this module never interacts
with a live page or submits an application.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

from services.applications.form_analysis import analyze_controls


MAX_BREEZY_MARKUP_LENGTH = 100_000
MAX_BREEZY_MARKUP_EVENTS = 4096
_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare", "cf-chl-",
    "datadome", "perimeterx", "arkose", "funcaptcha", "geetest", "bot-check",
)
_TEXT_CHALLENGE_MARKERS = tuple(marker for marker in _CHALLENGE_MARKERS if marker != "challenge")
_IDENTIFIERS = {
    "address": "address", "city": "city", "country": "country", "email": "email",
    "email address": "email", "emailaddress": "email", "first name": "first_name",
    "firstname": "first_name", "given name": "first_name", "full name": "full_name",
    "fullname": "full_name", "last name": "last_name", "lastname": "last_name",
    "phone": "phone", "phone number": "phone", "phonenumber": "phone",
    "postal code": "postal_code", "postalcode": "postal_code", "state": "state",
    "website": "website", "zip code": "zip_code", "zipcode": "zip_code",
}
_SAFE_TYPES = {"text", "email", "tel", "url", "number", "date", "select", "textarea"}
_TENANT = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I | re.ASCII)
_POSITION = re.compile(
    r"(?:[0-9a-f]{12}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"[a-z0-9]+(?:-[a-z0-9]+)*\Z", re.I | re.ASCII,
)


def _report(reason: str) -> dict:
    return {"status": "REVIEW_REQUIRED", "reason": reason, "fields": []}


def _safe_identifier(value):
    if not isinstance(value, str) or not value.isascii():
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return _IDENTIFIERS.get(normalized)


def _supported_breezy_route(route_url):
    if not isinstance(route_url, str) or not route_url or len(route_url) > 2048 or "\\" in route_url:
        return False
    try:
        parsed = urlsplit(route_url)
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme.casefold() != "https" or port is not None
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or not parsed.netloc.isascii()):
        return False
    host = parsed.hostname or ""
    if not host.isascii() or not host.casefold().endswith(".breezy.hr"):
        return False
    if not _TENANT.fullmatch(host[:-len(".breezy.hr")]):
        return False
    parts = parsed.path.split("/")
    return (len(parts) in (3, 4) and parts[0] == "" and parts[1] == "p"
            and (len(parts) == 3 or parts[3] == "apply")
            and _POSITION.fullmatch(parts[2]) is not None)


class _BreezyFormParser(HTMLParser):
    """Collect only static control metadata; discard values and page text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls = []
        self.requiredness = []
        self.labels_by_id = {}
        self.label_stack = []
        self.form_count = 0
        self.active_form = False
        self.outside_form_controls = False
        self.invalid_attributes = False
        self.challenge_protected = False
        self.events = 0

    def _challenge(self, value):
        text = str(value or "").casefold()
        if any(marker in text for marker in _CHALLENGE_MARKERS):
            self.challenge_protected = True

    def _count_event(self):
        self.events += 1
        if self.events > MAX_BREEZY_MARKUP_EVENTS:
            raise ValueError("MARKUP_EVENT_LIMIT_EXCEEDED")

    def handle_starttag(self, tag, attrs):
        self._count_event()
        tag = tag.casefold()
        keys = [str(key).casefold() for key, _ in attrs]
        if len(keys) != len(set(keys)):
            self.invalid_attributes = True
        attr_map = {}
        for key, value in attrs:
            normalized_key = str(key).casefold()
            normalized_value = str(value or "")
            self._challenge(normalized_key)
            self._challenge(normalized_value)
            if normalized_key not in attr_map:
                attr_map[normalized_key] = normalized_value

        if tag in {"script", "style", "noscript", "template"}:
            self.label_stack.append({"ignored": tag})
            return
        if self.label_stack and self.label_stack[-1].get("ignored"):
            return
        if tag == "form":
            self.form_count += 1
            self.active_form = True
            return
        if tag == "label" and self.active_form:
            self.label_stack.append({
                "for": attr_map.get("for"), "wrapped_index": None, "text": [],
            })
            return
        if tag in {"input", "select", "textarea"}:
            if not self.active_form:
                self.outside_form_controls = True
                return
            if len(self.controls) < 101:
                control = {"tag": tag, "labels": [], "options": []}
                for source, target in (("name", "name"), ("id", "id"),
                                       ("placeholder", "placeholder"),
                                       ("aria-label", "aria_label")):
                    if source in attr_map:
                        control[target] = attr_map[source]
                if tag == "input":
                    control["input_type"] = attr_map.get("type", "text") or "text"
                self.requiredness.append(
                    "required" in attr_map
                    or attr_map.get("aria-required", "").casefold() == "true"
                )
                for label in self.label_stack:
                    if "wrapped_index" in label:
                        label["wrapped_index"] = len(self.controls)
                self.controls.append(control)

    def handle_startendtag(self, tag, attrs):
        self._count_event()
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self._count_event()
        tag = tag.casefold()
        if self.label_stack and self.label_stack[-1].get("ignored") == tag:
            self.label_stack.pop()
            return
        if tag == "form":
            self.active_form = False
            return
        if tag == "label" and self.label_stack and "wrapped_index" in self.label_stack[-1]:
            label = self.label_stack.pop()
            text = " ".join("".join(label["text"]).split())
            if text:
                index = label["wrapped_index"]
                if index is not None and index < len(self.controls):
                    self.controls[index]["labels"].append(text)
                if label["for"]:
                    self.labels_by_id.setdefault(label["for"], []).append(text)

    def handle_data(self, data):
        self._count_event()
        text = str(data or "")
        if any(marker in text.casefold() for marker in _TEXT_CHALLENGE_MARKERS):
            self.challenge_protected = True
        if self.label_stack and "text" in self.label_stack[-1]:
            self.label_stack[-1]["text"].append(text)

    def finish(self):
        for control in self.controls:
            control_id = control.get("id")
            if control_id:
                control["labels"].extend(self.labels_by_id.get(control_id, []))
        return self.controls


def analyze_breezy_form_html(html, *, route_url) -> dict:
    """Return a sanitized, bounded projection for one public Breezy route.

    Raw values, labels and arbitrary prompt text are used only while analyzing
    locally and never appear in the returned report. The caller owns fetching.
    """
    if isinstance(html, bytes):
        if len(html) > MAX_BREEZY_MARKUP_LENGTH:
            return _report("MARKUP_LIMIT_EXCEEDED")
        try:
            source = html.decode("utf-8")
        except UnicodeDecodeError:
            return _report("INVALID_FORM_HTML")
    elif isinstance(html, str):
        if len(html) > MAX_BREEZY_MARKUP_LENGTH:
            return _report("MARKUP_LIMIT_EXCEEDED")
        source = html
    else:
        return _report("INVALID_FORM_HTML")

    if not _supported_breezy_route(route_url):
        return _report("ROUTE_UNSUPPORTED")

    parser = _BreezyFormParser()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        return _report("MARKUP_PARSE_FAILED")
    if parser.invalid_attributes:
        return _report("MARKUP_PARSE_FAILED")
    if parser.form_count == 0:
        return _report("NO_FORM")
    if parser.form_count != 1 or parser.outside_form_controls:
        return _report("AMBIGUOUS_FORM")
    if parser.challenge_protected:
        return _report("CHALLENGE_PROTECTED")

    analysis = analyze_controls(parser.finish())
    if analysis.get("status") != "PASS":
        return _report(analysis.get("reason") or "FORM_ANALYSIS_UNKNOWN")

    fields = []
    for index, field in enumerate(analysis["fields"]):
        raw_type = field.get("input_type") or field.get("tag")
        normalized_type = raw_type.casefold() if isinstance(raw_type, str) else ""
        if normalized_type not in _SAFE_TYPES:
            normalized_type = "other"
        fields.append({
            "id": _safe_identifier(field.get("id")),
            "name": _safe_identifier(field.get("name")),
            "type": normalized_type,
            "required": index < len(parser.requiredness) and parser.requiredness[index],
            "review_reason": "STATIC_VISIBILITY_UNVERIFIED",
        })
    return {
        "status": "REVIEW_REQUIRED",
        "reason": "STATIC_VISIBILITY_UNVERIFIED",
        "fields": fields,
    }
