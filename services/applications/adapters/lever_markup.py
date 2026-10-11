"""Value-free, bounded extraction of static Lever form control metadata."""
from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser

from services.applications.form_analysis import analyze_controls


MAX_MARKUP_LENGTH = 100_000
_CONTROL_KEYS = {
    "tag", "label", "labels", "name", "id", "placeholder", "aria_label",
    "input_type", "nearby_text", "section_heading", "fieldset_legend", "options",
    "unsafe_text_context",
}
_DISCARDED_KEYS = {
    "value", "current_value", "selected_value", "checked", "answer", "answers",
    "current_answer", "selected_answer",
}
_HTML_ATTRIBUTES = {"name", "id", "for", "placeholder", "aria-label", "type"}
_IGNORED_CONTENT = {"script", "style", "noscript", "template"}
_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "challenge", "cloudflare",
    "cf-chl-", "datadome", "perimeterx", "arkose", "funcaptcha", "geetest", "bot-check",
)
_TEXT_CHALLENGE_MARKERS = tuple(marker for marker in _CHALLENGE_MARKERS
                                if marker != "challenge")
_INTERACTIVE_ROLES = {
    "button", "checkbox", "combobox", "listbox", "option", "radio", "searchbox",
    "slider", "spinbutton", "switch", "textbox",
}


def _unknown(reason: str) -> dict:
    return {"status": "UNKNOWN", "reason": reason, "fields": []}


def _project_controls(controls):
    if not isinstance(controls, list):
        return controls, []
    projected = []
    requiredness = []
    for control in controls:
        if not isinstance(control, Mapping):
            projected.append(control)
            requiredness.append(False)
            continue
        clean = {}
        invalid = False
        required = control.get("required") is True
        requiredness.append(required)
        for key, value in control.items():
            normalized = str(key).casefold()
            if normalized in _DISCARDED_KEYS or normalized == "required":
                continue
            if normalized not in _CONTROL_KEYS:
                invalid = True
                continue
            clean[normalized] = value
        if invalid:
            clean["__invalid_metadata__"] = True
        projected.append(clean)
    return projected, requiredness


class _StaticFormParser(HTMLParser):
    """Collect labels/options attached to controls, ignoring all form values."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.controls = []
        self.labels_by_id = {}
        self.label_stack = []
        self.option_index = None
        self.select_index = None
        self.ignored_stack = []
        self.challenge_protected = False
        self.unsupported_interactive_control = False
        self.requiredness = []
        self.invalid_attributes = False
        self.form_count = 0
        self.active_form = False
        self.outside_form_controls = False

    def _challenge(self, value):
        text = str(value or "").casefold()
        if any(marker in text for marker in _CHALLENGE_MARKERS):
            self.challenge_protected = True

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        names = [str(key).casefold() for key, _ in attrs]
        if len(names) != len(set(names)):
            self.invalid_attributes = True
        attr_map = {}
        for key, value in attrs:
            key = key.casefold()
            if key in _HTML_ATTRIBUTES and key not in attr_map:
                attr_map[key] = value
            self._challenge(key)
            self._challenge(value)

        all_attrs = {str(key).casefold(): str(value or "") for key, value in attrs}

        if tag in _IGNORED_CONTENT:
            self.ignored_stack.append(tag)
            return
        if self.ignored_stack:
            return

        if tag == "form":
            self.form_count += 1
            self.active_form = True
            return
        if not self.active_form:
            if tag in {"input", "select", "textarea"}:
                self.outside_form_controls = True
            return

        role = all_attrs.get("role", "").casefold()
        if (role in _INTERACTIVE_ROLES
                or ("contenteditable" in all_attrs
                    and all_attrs["contenteditable"].casefold() != "false")):
            self.unsupported_interactive_control = True

        if tag in {"input", "select", "textarea"}:
            if len(self.controls) < 101:
                control = {"tag": tag, "labels": [], "options": []}
                for key in ("name", "id", "placeholder"):
                    if key in attr_map:
                        control[key] = attr_map[key]
                if "aria-label" in attr_map:
                    control["aria_label"] = attr_map["aria-label"]
                if tag == "input":
                    control["input_type"] = attr_map.get("type") or "text"
                self.requiredness.append(
                    "required" in all_attrs
                    or all_attrs.get("aria-required", "").casefold() == "true"
                )
                self.controls.append(control)
                if tag == "select":
                    self.select_index = len(self.controls) - 1
                for label in self.label_stack:
                    label["wrapped_index"] = len(self.controls) - 1

        if tag == "label":
            self.label_stack.append({
                "for": attr_map.get("for"), "wrapped_index": None, "text": []
            })
        elif tag == "option" and self.select_index is not None:
            self.option_index = self.select_index
            if self.option_index < len(self.controls):
                self.controls[self.option_index]["options"].append("")

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if self.ignored_stack:
            if tag == self.ignored_stack[-1]:
                self.ignored_stack.pop()
            return
        if tag == "form":
            self.active_form = False
            return
        if not self.active_form:
            return
        if tag == "option":
            self.option_index = None
        elif tag == "select":
            self.select_index = None
        elif tag == "label" and self.label_stack:
            label = self.label_stack.pop()
            text = " ".join("".join(label["text"]).split())
            if text:
                wrapped = label["wrapped_index"]
                if wrapped is not None and wrapped < len(self.controls):
                    self.controls[wrapped]["labels"].append(text)
                if label["for"]:
                    self.labels_by_id.setdefault(label["for"], []).append(text)

    def handle_data(self, data):
        text = str(data or "").casefold()
        if any(marker in text for marker in _TEXT_CHALLENGE_MARKERS):
            self.challenge_protected = True
        if self.ignored_stack:
            return
        if not self.active_form:
            return
        for label in self.label_stack:
            label["text"].append(data)
        if self.option_index is not None and self.option_index < len(self.controls):
            options = self.controls[self.option_index]["options"]
            if options:
                options[-1] += data

    def finish(self):
        for control in self.controls:
            control_id = control.get("id")
            if control_id:
                control["labels"].extend(self.labels_by_id.get(control_id, []))
        return self.controls


def analyze_lever_markup(source) -> dict:
    """Analyze supplied static controls or HTML, returning shared safe metadata.

    Mapping controls are projected to the form analyzer's allowlist. Current
    values and answers are discarded before analysis. HTML input is parsed
    locally; only static control attributes, label text, and option labels are
    retained. Page text is treated as untrusted data and is never executed.
    """
    if isinstance(source, str):
        if len(source) > MAX_MARKUP_LENGTH:
            return _unknown("MARKUP_LIMIT_EXCEEDED")
        parser = _StaticFormParser()
        try:
            parser.feed(source)
            parser.close()
            controls = parser.finish()
        except Exception:
            return _unknown("MARKUP_PARSE_FAILED")
        if parser.invalid_attributes:
            return _unknown("MARKUP_PARSE_FAILED")
        if parser.form_count == 0:
            return _unknown("NO_FORM")
        if parser.form_count != 1:
            return _unknown("AMBIGUOUS_FORM")
        if parser.outside_form_controls:
            return _unknown("AMBIGUOUS_FORM")
        if parser.challenge_protected:
            return _unknown("CHALLENGE_PROTECTED")
        if parser.unsupported_interactive_control:
            return _unknown("UNSUPPORTED_INTERACTIVE_CONTROL")
        requiredness = parser.requiredness
    else:
        controls = source
        requiredness = None
    projected, projected_requiredness = _project_controls(controls)
    if requiredness is None:
        requiredness = projected_requiredness
    analysis = analyze_controls(projected)
    fields = analysis.get("fields")
    if isinstance(fields, list):
        for index, field in enumerate(fields):
            if isinstance(field, dict):
                field["required"] = (requiredness[index]
                                     if index < len(requiredness) else False)
    return analysis
