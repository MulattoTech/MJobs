"""Strict, metadata-only analyzer for synthetic Workday form descriptions.

This module does not inspect a page or accept applicant answers. It consumes a
versioned metadata envelope supplied by a caller and fails closed whenever the
envelope or a field's semantics are not understood.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from html.parser import HTMLParser

from services.applications.form_analysis import analyze_controls


_FORM_KEYS = {"provider", "schema_version", "form_id", "revision", "fields"}
_FIELD_KEYS = {
    "id", "label", "type", "required", "conditional", "conditional_on",
    "repeatable", "repeatable_group",
}
_REQUIRED_FIELD_KEYS = {"id", "label", "type", "required", "conditional", "repeatable"}
_FORBIDDEN_KEYS = {
    "value", "current_value", "selected_value", "checked", "answer", "answers",
    "default_value", "defaultvalue", "input_value", "text_content",
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SAFE_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SUPPORTED_TYPES = {
    "text", "email", "tel", "url", "number", "date", "select", "textarea",
    "checkbox", "radio", "file",
}
MAX_WORKDAY_MARKUP_LENGTH = 2 * 1024 * 1024
_HTML_ATTRIBUTES = {
    "id", "name", "type", "placeholder", "aria-label", "aria-required",
    "for", "class", "role", "contenteditable", "data-automation-id",
    "data-workday-conditional", "data-workday-conditional-on",
    "data-workday-repeatable", "data-workday-repeatable-group",
}
_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare",
    "cf-chl-", "datadome", "perimeterx", "arkose", "funcaptcha", "geetest",
)
_IGNORED_CONTENT = {"script", "style", "noscript", "template"}
_INTERACTIVE_ROLES = {
    "button", "checkbox", "combobox", "listbox", "option", "radio", "searchbox",
    "slider", "spinbutton", "switch", "textbox",
}
WORKDAY_FORM_CAPABILITIES = {
    "metadata_analysis": True,
    "human_review_for_uncertainty": True,
    "page_interaction": False,
    "applicant_value_reading": False,
    "autofill": False,
    "document_upload": False,
    "submission": False,
}


def _unknown(reason: str) -> dict:
    return {"provider": "workday", "status": "UNKNOWN", "reason": reason, "fields": []}


def _normalized_text(value, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("INVALID_FIELD_METADATA")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError("INVALID_FIELD_METADATA")
    return value or None


def _has_forbidden_keys(value) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(str(key).casefold() in _FORBIDDEN_KEYS for key in value)


def _sensitivity(field_id: str, label: str, field_type: str) -> str:
    tag = "select" if field_type == "select" else "textarea" if field_type == "textarea" else "input"
    result = analyze_controls([{
        "tag": tag,
        "id": field_id,
        "label": label or "",
        "input_type": field_type,
    }])
    if result.get("status") != "PASS" or len(result.get("fields", [])) != 1:
        return "UNKNOWN"
    return result["fields"][0].get("sensitivity", "UNKNOWN")


def analyze_workday_form(metadata: Mapping) -> dict:
    """Normalize a strict Workday metadata envelope without reading answer data.

    ``metadata`` must contain ``provider``, schema version 1, stable form and
    revision identifiers, and a list of field metadata. Every field must state
    its required, conditional, and repeatable flags explicitly. Conditional and
    repeated fields are described, but always require human review.
    """
    if not isinstance(metadata, Mapping):
        return _unknown("INVALID_FORM_METADATA")
    if _has_forbidden_keys(metadata):
        return _unknown("FORBIDDEN_VALUE_METADATA")
    if set(metadata) != _FORM_KEYS:
        return _unknown("UNSUPPORTED_FORM_METADATA")
    if metadata.get("provider") != "workday" or metadata.get("schema_version") != 1:
        return _unknown("UNSUPPORTED_FORM_SCHEMA")

    form_id = metadata.get("form_id")
    revision = metadata.get("revision")
    if (not isinstance(form_id, str) or not _IDENTIFIER.fullmatch(form_id)
            or not isinstance(revision, str) or not _SAFE_REVISION.fullmatch(revision)):
        return _unknown("INVALID_FORM_IDENTITY")
    raw_fields = metadata.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > 100:
        return _unknown("INVALID_FIELD_LIST")

    parsed = []
    identities = set()
    review_reasons = set()
    try:
        for raw in raw_fields:
            if not isinstance(raw, Mapping):
                raise ValueError("INVALID_FIELD_METADATA")
            if _has_forbidden_keys(raw):
                raise ValueError("FORBIDDEN_VALUE_METADATA")
            if not _REQUIRED_FIELD_KEYS.issubset(raw) or not set(raw).issubset(_FIELD_KEYS):
                raise ValueError("UNSUPPORTED_FIELD_METADATA")

            field_id = _normalized_text(raw.get("id"), required=True)
            label = _normalized_text(raw.get("label"), required=False)
            field_type = _normalized_text(raw.get("type"), required=True)
            if not _IDENTIFIER.fullmatch(field_id):
                raise ValueError("INVALID_FIELD_ID")
            identity = field_id.casefold()
            if identity in identities:
                raise ValueError("DUPLICATE_FIELD_ID")
            identities.add(identity)
            if field_type.casefold() != field_type:
                raise ValueError("UNSUPPORTED_FIELD_TYPE")
            field_type = field_type.casefold()
            if type(raw.get("required")) is not bool:
                raise ValueError("INVALID_FIELD_FLAGS")
            for flag in ("conditional", "repeatable"):
                if raw.get(flag) is not None and type(raw.get(flag)) is not bool:
                    raise ValueError("INVALID_FIELD_FLAGS")

            conditional_on = raw.get("conditional_on")
            if raw["conditional"] is None:
                if conditional_on is not None:
                    raise ValueError("INVALID_CONDITIONAL_METADATA")
                review_reasons.add("CONDITIONAL_SEMANTICS_UNOBSERVED")
            elif raw["conditional"]:
                if (not isinstance(conditional_on, str)
                        or not _IDENTIFIER.fullmatch(conditional_on)):
                    raise ValueError("INVALID_CONDITIONAL_METADATA")
                review_reasons.add("CONDITIONAL_FIELD")
            elif conditional_on is not None:
                raise ValueError("UNEXPECTED_CONDITIONAL_METADATA")

            repeatable_group = raw.get("repeatable_group")
            if raw["repeatable"] is None:
                if repeatable_group is not None:
                    raise ValueError("INVALID_REPEATABLE_METADATA")
                review_reasons.add("REPEATABLE_SEMANTICS_UNOBSERVED")
            elif raw["repeatable"]:
                if (not isinstance(repeatable_group, str)
                        or not _IDENTIFIER.fullmatch(repeatable_group)):
                    raise ValueError("INVALID_REPEATABLE_METADATA")
                review_reasons.add("REPEATABLE_FIELD")
            elif repeatable_group is not None:
                raise ValueError("UNEXPECTED_REPEATABLE_METADATA")

            sensitivity = _sensitivity(field_id, label, field_type)
            supported = field_type in _SUPPORTED_TYPES
            if not supported:
                review_reasons.add("UNSUPPORTED_FIELD_TYPE")
            if sensitivity != "STANDARD":
                review_reasons.add("SENSITIVE_FIELD")
            parsed.append({
                "id": field_id,
                "label": label,
                "type": field_type,
                "required": raw["required"],
                "conditional": raw["conditional"],
                "conditional_on": conditional_on,
                "repeatable": raw["repeatable"],
                "repeatable_group": repeatable_group,
                "sensitivity": sensitivity,
                "supported": supported,
                "requires_human_review": (
                    raw["conditional"] is not False or raw["repeatable"] is not False
                    or sensitivity != "STANDARD"
                    or not supported
                ),
            })
    except ValueError as error:
        return _unknown(str(error))

    id_set = {field["id"].casefold() for field in parsed}
    if any(field["conditional_on"] and field["conditional_on"].casefold() not in id_set
           for field in parsed):
        return _unknown("UNKNOWN_CONDITIONAL_CONTROLLER")

    if review_reasons:
        status = "REVIEW_REQUIRED"
        reason = ",".join(sorted(review_reasons))
    else:
        status = "PASS"
        reason = None
    return {
        "provider": "workday",
        "schema_version": 1,
        "status": status,
        "reason": reason,
        "form_id": form_id,
        "revision": revision,
        "fields": parsed,
    }


class _WorkdayMarkupParser(HTMLParser):
    """Collect only static Workday control metadata and visible label text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.form_count = 0
        self.active_form = False
        self.controls = []
        self.labels_by_id = {}
        self.label_stack = []
        self.ignored_stack = []
        self.value_text_depth = 0
        self.challenge_protected = False
        self.unsupported_interactive = False
        self.outside_form_controls = False
        self.invalid = False

    def _attrs(self, attrs):
        names = [str(name).casefold() for name, _ in attrs]
        if len(names) != len(set(names)):
            self.invalid = True
            return {}
        # Values for answer-bearing attributes (value, checked, selected, etc.)
        # are intentionally not copied or inspected.
        return {
            str(name).casefold(): value
            for name, value in attrs
            if str(name).casefold() in _HTML_ATTRIBUTES
        }

    def _check_challenge_metadata(self, attrs):
        for name in ("class", "id", "role", "data-automation-id"):
            value = attrs.get(name)
            if not isinstance(value, str):
                continue
            normalized = value.casefold()
            if any(marker in normalized for marker in _CHALLENGE_MARKERS):
                self.challenge_protected = True

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        values = self._attrs(attrs)
        self._check_challenge_metadata(values)
        if tag in _IGNORED_CONTENT:
            self.ignored_stack.append(tag)
            return
        if self.ignored_stack:
            return
        if tag == "form":
            self.form_count += 1
            self.active_form = self.form_count == 1
            return
        if tag == "label":
            self.label_stack.append({"for": values.get("for"), "parts": [], "index": None})
            return
        if tag == "textarea":
            self.value_text_depth += 1
        if not self.active_form:
            if tag in {"input", "select", "textarea"}:
                input_type = values.get("type", "text") if tag == "input" else tag
                if input_type != "hidden":
                    self.outside_form_controls = True
            return

        role = values.get("role", "")
        if isinstance(role, str) and role.casefold() in _INTERACTIVE_ROLES:
            self.unsupported_interactive = True
        contenteditable = values.get("contenteditable")
        if isinstance(contenteditable, str) and contenteditable.casefold() != "false":
            self.unsupported_interactive = True

        if tag not in {"input", "select", "textarea"}:
            return
        input_type = values.get("type", "text") if tag == "input" else tag
        if tag == "input" and input_type.casefold() in {"hidden", "submit", "button", "reset", "image"}:
            return
        if len(self.controls) >= 101:
            self.invalid = True
            return
        conditional = values.get("data-workday-conditional")
        repeatable = values.get("data-workday-repeatable")
        self.controls.append({
            "id": values.get("id"),
            "name": values.get("name"),
            "placeholder": values.get("placeholder"),
            "aria_label": values.get("aria-label"),
            "type": input_type.casefold() if isinstance(input_type, str) else "",
            "required": (
                any(str(name).casefold() == "required" for name, _ in attrs)
                or (isinstance(values.get("aria-required"), str)
                    and values["aria-required"].casefold() == "true")
            ),
            "conditional": self._semantic_flag(conditional),
            "conditional_on": values.get("data-workday-conditional-on"),
            "repeatable": self._semantic_flag(repeatable),
            "repeatable_group": values.get("data-workday-repeatable-group"),
            "labels": [],
        })
        for label in self.label_stack:
            label["index"] = len(self.controls) - 1

    @staticmethod
    def _semantic_flag(value):
        if not isinstance(value, str):
            return None
        normalized = value.casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        return "invalid"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if self.ignored_stack:
            if tag == self.ignored_stack[-1]:
                self.ignored_stack.pop()
            return
        if tag == "form":
            self.active_form = False
        elif tag == "textarea" and self.value_text_depth:
            self.value_text_depth -= 1
        elif tag == "label" and self.label_stack:
            label = self.label_stack.pop()
            text = " ".join(" ".join(label["parts"]).split())
            if len(text) > 500:
                self.invalid = True
                return
            index = label["index"]
            if text and index is not None and index < len(self.controls):
                self.controls[index]["labels"].append(text)
            field_id = label["for"]
            if text and isinstance(field_id, str):
                self.labels_by_id.setdefault(field_id, []).append(text)

    def handle_data(self, data):
        if self.ignored_stack or self.value_text_depth:
            return
        for label in self.label_stack:
            if sum(map(len, label["parts"])) + len(data) > 500:
                self.invalid = True
            else:
                label["parts"].append(data)

    def finish(self):
        if (self.invalid or self.label_stack or self.ignored_stack or self.value_text_depth
                or self.active_form):
            return _unknown("INVALID_WORKDAY_MARKUP")
        if self.form_count == 0:
            return _unknown("NO_FORM")
        if self.form_count != 1 or self.outside_form_controls:
            return _unknown("AMBIGUOUS_FORM")
        if self.challenge_protected:
            return _unknown("CHALLENGE_PROTECTED")
        if self.unsupported_interactive:
            return _unknown("UNSUPPORTED_INTERACTIVE_CONTROL")
        if not self.controls:
            return _unknown("NO_FORM_CONTROLS")

        for control in self.controls:
            field_id = control.get("id")
            if isinstance(field_id, str):
                control["labels"].extend(self.labels_by_id.get(field_id, []))
        fields = []
        projections = []
        for index, control in enumerate(self.controls):
            source_id = control.get("id")
            name = control.get("name")
            internal_id = source_id or name or f"field-{index}"
            labels = control["labels"]
            label = " ".join(labels)
            if not label:
                label = control.get("aria_label") or control.get("placeholder")
            fields.append({
                "id": internal_id,
                "label": label,
                "type": control["type"],
                "required": control["required"],
                "conditional": control["conditional"],
                "conditional_on": control.get("conditional_on"),
                "repeatable": control["repeatable"],
                "repeatable_group": control.get("repeatable_group"),
            })
            projections.append({"id": source_id, "name": name})

        analysis = analyze_workday_form({
            "provider": "workday",
            "schema_version": 1,
            "form_id": "workday-static-form",
            "revision": "static-markup-v1",
            "fields": fields,
        })
        if analysis.get("status") != "UNKNOWN":
            for analyzed, source in zip(analysis["fields"], projections):
                analyzed["id"] = source["id"]
                analyzed["name"] = source["name"]
        return analysis


def analyze_workday_form_html(source) -> dict:
    """Extract Workday control metadata from supplied static HTML only.

    Dynamic semantics are unknown unless explicitly stated with
    ``data-workday-conditional`` and ``data-workday-repeatable`` metadata.
    Input, selected, default, and textarea answer values are discarded.
    """
    if isinstance(source, bytes):
        if len(source) > MAX_WORKDAY_MARKUP_LENGTH:
            return _unknown("MARKUP_LIMIT_EXCEEDED")
        try:
            markup = source.decode("utf-8")
        except UnicodeDecodeError:
            return _unknown("INVALID_WORKDAY_MARKUP")
    elif isinstance(source, str):
        try:
            encoded = source.encode("utf-8")
        except UnicodeEncodeError:
            return _unknown("INVALID_WORKDAY_MARKUP")
        if len(encoded) > MAX_WORKDAY_MARKUP_LENGTH:
            return _unknown("MARKUP_LIMIT_EXCEEDED")
        markup = source
    else:
        return _unknown("INVALID_WORKDAY_MARKUP")
    parser = _WorkdayMarkupParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return _unknown("MARKUP_PARSE_FAILED")
    if any(
        field[key] == "invalid"
        for field in parser.controls
        for key in ("conditional", "repeatable")
    ):
        return _unknown("INVALID_DYNAMIC_METADATA")
    return parser.finish()


def requires_workday_human_review(analysis) -> bool:
    """Return true unless every field has supported, unchanged known semantics."""
    if (not isinstance(analysis, Mapping) or analysis.get("provider") != "workday"
            or analysis.get("status") != "PASS"):
        return True
    fields = analysis.get("fields")
    if not isinstance(fields, list) or not fields:
        return True
    for field in fields:
        if (not isinstance(field, Mapping) or field.get("supported") is not True
                or field.get("requires_human_review") is not False
                or field.get("sensitivity") != "STANDARD"
                or field.get("conditional") is not False
                or field.get("repeatable") is not False):
            return True
    return False


def validate_workday_form(metadata: Mapping, expected_analysis: Mapping) -> dict:
    """Reanalyze metadata and reject any schema, revision, or field change.

    Only the analyzer's safe projection is compared. A malformed prior snapshot
    or any mismatch produces a fixed value-free UNKNOWN result.
    """
    if (not isinstance(expected_analysis, Mapping)
            or expected_analysis.get("provider") != "workday"
            or expected_analysis.get("status") not in {"PASS", "REVIEW_REQUIRED"}
            or not isinstance(expected_analysis.get("fields"), list)
            or not expected_analysis.get("fields")):
        return _unknown("INVALID_EXPECTED_ANALYSIS")
    current = analyze_workday_form(metadata)
    if current.get("status") == "UNKNOWN":
        return current
    if current != expected_analysis:
        return _unknown("FORM_CHANGED")
    return current
