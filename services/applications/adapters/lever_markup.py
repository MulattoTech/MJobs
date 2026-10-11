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


def _unknown(reason: str) -> dict:
    return {"status": "UNKNOWN", "reason": reason, "fields": []}


def _project_controls(controls):
    if not isinstance(controls, list):
        return controls
    projected = []
    for control in controls:
        if not isinstance(control, Mapping):
            projected.append(control)
            continue
        clean = {}
        invalid = False
        for key, value in control.items():
            normalized = str(key).casefold()
            if normalized in _DISCARDED_KEYS:
                continue
            if normalized not in _CONTROL_KEYS:
                invalid = True
                continue
            clean[normalized] = value
        if invalid:
            clean["__invalid_metadata__"] = True
        projected.append(clean)
    return projected


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

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attr_map = {}
        for key, value in attrs:
            key = key.casefold()
            if key in _HTML_ATTRIBUTES and key not in attr_map:
                attr_map[key] = value

        if tag in _IGNORED_CONTENT:
            self.ignored_stack.append(tag)
            return
        if self.ignored_stack:
            return

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
        if self.ignored_stack:
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
    else:
        controls = source
    return analyze_controls(_project_controls(controls))
