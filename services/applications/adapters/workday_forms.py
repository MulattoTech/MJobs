"""Strict, value-free analyzer for Workday form metadata.

Static HTML analysis and the browser-side DOM projection both discard applicant
answers. The normalized analyzer fails closed whenever field semantics are
unknown or unsupported.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from html.parser import HTMLParser

from services.applications.form_analysis import analyze_controls


_FORM_KEYS = {"provider", "schema_version", "form_id", "revision", "fields"}
_FIELD_KEYS = {
    "id", "name", "label", "type", "required", "conditional", "conditional_on",
    "repeatable", "repeatable_group",
    "options_fingerprint",
}
_REQUIRED_FIELD_KEYS = {"id", "label", "type", "required", "conditional", "repeatable"}
_REPEAT_INDEX = re.compile(r"(?:\[\d+\]|\.\d+)(?=\.|$)")
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
MAX_WORKDAY_MARKUP_EVENTS = 4096
MAX_WORKDAY_DOM_FIELDS = 100
MAX_WORKDAY_LABEL_DEPTH = 32
_HTML_ATTRIBUTES = {
    "id", "name", "type", "placeholder", "aria-label", "aria-required",
    "for", "class", "role", "contenteditable", "data-automation-id",
    "aria-controls",
    "data-workday-conditional", "data-workday-conditional-on",
    "data-workday-repeatable", "data-workday-repeatable-group",
}
_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare",
    "cf-chl-", "datadome", "perimeterx", "arkose", "funcaptcha", "geetest",
)
_IGNORED_CONTENT = {"script", "style", "noscript", "template", "option", "optgroup"}
_INTERACTIVE_ROLES = {
    "button", "checkbox", "combobox", "listbox", "option", "radio", "searchbox",
    "slider", "spinbutton", "switch", "textbox",
}
WORKDAY_DOM_METADATA_SCRIPT = r"""
async forms => {
  const MAX_WORKDAY_MARKUP_EVENTS = 4096;
  const MAX_WORKDAY_DOM_FIELDS = 100;
  const MAX_WORKDAY_OPTIONS = 100;
  const fail = reason => ({status: "UNKNOWN", reason, fields: []});
  const nodes = Array.from(document.querySelectorAll("*"));
  if (nodes.length > MAX_WORKDAY_MARKUP_EVENTS) return fail("MARKUP_LIMIT_EXCEEDED");
  const challengeMarkers = [
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare", "cf-chl-",
    "datadome", "perimeterx", "arkose", "funcaptcha", "geetest"
  ];
  for (const node of nodes) {
    for (const name of ["class", "id", "role", "data-automation-id"]) {
      const text = (node.getAttribute(name) || "").toLowerCase();
      if (challengeMarkers.some(marker => text.includes(marker))) {
        return fail("CHALLENGE_PROTECTED");
      }
    }
  }
  if (forms.length !== 1) return fail(forms.length ? "AMBIGUOUS_FORM" : "NO_FORM");
  const form = forms[0];
  const controls = Array.from(form.querySelectorAll("input,select,textarea"));
  const isRendered = node => {
    for (let current = node; current && current !== form.parentElement; current = current.parentElement) {
      if (current.hidden || current.getAttribute("aria-hidden") === "true") return false;
      const style = getComputedStyle(current);
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || Number(style.opacity) === 0) return false;
    }
    return node.getClientRects().length > 0;
  };
  const visibleControls = controls.filter(node =>
    !(node.tagName.toLowerCase() === "input" &&
      ["hidden", "submit", "button", "reset", "image"].includes(
        (node.getAttribute("type") || "text").toLowerCase()
      )) && isRendered(node)
  );
  if (visibleControls.length > MAX_WORKDAY_DOM_FIELDS) return fail("FIELD_LIMIT_EXCEEDED");
  const outsideControls = Array.from(document.querySelectorAll("input,select,textarea"))
    .some(node => !form.contains(node) &&
      !(node.tagName.toLowerCase() === "input" &&
        (node.getAttribute("type") || "text").toLowerCase() === "hidden"));
  if (outsideControls) return fail("AMBIGUOUS_FORM");
  for (const node of form.querySelectorAll("[role],[contenteditable]")) {
    const role = (node.getAttribute("role") || "").toLowerCase();
    if (["button", "checkbox", "combobox", "listbox", "option", "radio",
         "searchbox", "slider", "spinbutton", "switch", "textbox"].includes(role)) {
      return fail("UNSUPPORTED_INTERACTIVE_CONTROL");
    }
    const editable = node.getAttribute("contenteditable");
    if (editable !== null && editable.toLowerCase() !== "false") {
      return fail("UNSUPPORTED_INTERACTIVE_CONTROL");
    }
  }
  const controllerByTarget = new Map();
  for (const node of visibleControls) {
    const id = node.getAttribute("id");
    const targets = node.getAttribute("aria-controls");
    if (id && targets) for (const target of targets.trim().split(/\s+/)) {
      if (target) controllerByTarget.set(target, id);
    }
  }
  const semanticFlag = value => {
    if (value === null) return null;
    const normalized = value.toLowerCase();
    if (normalized === "true") return true;
    if (normalized === "false") return false;
    return "invalid";
  };
  const normalizeLabel = text => text.replace(/\s+/gu, " ").trim();
  const fingerprint = async labels => {
    const canonical = Array.from(JSON.stringify(labels), char => {
      const code = char.codePointAt(0);
      if (code <= 0x7f) return char;
      if (code <= 0xffff) return `\\u${code.toString(16).padStart(4, "0")}`;
      const high = Math.floor((code - 0x10000) / 0x400) + 0xd800;
      const low = ((code - 0x10000) % 0x400) + 0xdc00;
      return `\\u${high.toString(16)}\\u${low.toString(16)}`;
    }).join("");
    const bytes = new TextEncoder().encode(canonical);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  };
  const fields = [];
  for (const node of visibleControls) {
    const tag = node.tagName.toLowerCase();
    const type = tag === "input" ? (node.getAttribute("type") || "text").toLowerCase() : tag;
    let id = node.getAttribute("id");
    const name = node.getAttribute("name");
    if (!id) id = name || `field-${fields.length}`;
    const labels = [];
    if (node.labels) for (const label of node.labels) {
      const walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
      let text = "";
      let part;
      while ((part = walker.nextNode())) {
        const parent = part.parentElement;
        if (parent && parent.closest("option,optgroup,textarea,script,style,noscript,template")) continue;
        text += part.nodeValue || "";
        if (text.length > 500) return fail("INVALID_FIELD_METADATA");
      }
      const normalized = normalizeLabel(text);
      if (normalized) labels.push(normalized);
    }
    const label = labels.join(" ") || node.getAttribute("aria-label") ||
      node.getAttribute("placeholder") || "";
    if (label.length > 500) return fail("INVALID_FIELD_METADATA");
    const explicitConditional = semanticFlag(node.getAttribute("data-workday-conditional"));
    const explicitRepeatable = semanticFlag(node.getAttribute("data-workday-repeatable"));
    if (explicitConditional === "invalid" || explicitRepeatable === "invalid") {
      return fail("INVALID_DYNAMIC_METADATA");
    }
    let conditionalOn = node.getAttribute("data-workday-conditional-on");
    for (let ancestor = node; ancestor && ancestor !== form; ancestor = ancestor.parentElement) {
      const controller = controllerByTarget.get(ancestor.getAttribute("id"));
      if (controller) { conditionalOn = controller; break; }
    }
    const conditional = conditionalOn ? true : explicitConditional;
    const repeatable = explicitRepeatable;
    const repeatableGroup = node.getAttribute("data-workday-repeatable-group");
    const field = {
      id, name, label, type,
      required: node.hasAttribute("required") ||
        (node.getAttribute("aria-required") || "").toLowerCase() === "true",
      conditional, conditional_on: conditionalOn,
      repeatable, repeatable_group: repeatableGroup
    };
    if (tag === "select") {
      const options = Array.from(node.options);
      if (options.length > MAX_WORKDAY_OPTIONS) return fail("OPTION_LIMIT_EXCEEDED");
      const labels = options.map(option => {
        const text = normalizeLabel(option.label || option.textContent || "");
        if (text.length > 500) return null;
        return text;
      });
      if (labels.some(label => label === null)) return fail("INVALID_FIELD_METADATA");
      field.options_fingerprint = await fingerprint(labels);
    }
    fields.push(field);
  }
  if (!fields.length) return fail("NO_FORM_CONTROLS");
  return {fields};
}
"""
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
            field_name = _normalized_text(raw.get("name"), required=False)
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

            options_fingerprint = raw.get("options_fingerprint")
            if options_fingerprint is not None and (
                    field_type != "select"
                    or not isinstance(options_fingerprint, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", options_fingerprint)):
                raise ValueError("INVALID_OPTIONS_FINGERPRINT")

            sensitivity = _sensitivity(field_id, label, field_type)
            supported = field_type in _SUPPORTED_TYPES
            if not supported:
                review_reasons.add("UNSUPPORTED_FIELD_TYPE")
            if sensitivity != "STANDARD":
                review_reasons.add("SENSITIVE_FIELD")
            parsed_field = {
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
            }
            if "name" in raw:
                parsed_field["name"] = field_name
            if options_fingerprint is not None:
                parsed_field["options_fingerprint"] = options_fingerprint
            parsed.append(parsed_field)
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


def analyze_workday_dom_metadata(metadata: Mapping) -> dict:
    """Normalize the browser's bounded, allowlisted Workday field projection."""
    if not isinstance(metadata, Mapping):
        return _unknown("INVALID_DOM_METADATA")
    if metadata.get("status") == "UNKNOWN":
        reason = metadata.get("reason")
        if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason):
            return _unknown(reason)
        return _unknown("INVALID_DOM_METADATA")
    if set(metadata) != {"fields"}:
        return _unknown("INVALID_DOM_METADATA")
    fields = metadata.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > MAX_WORKDAY_DOM_FIELDS:
        return _unknown("INVALID_FIELD_LIST")
    for field in fields:
        if not isinstance(field, Mapping) or not set(field).issubset(_FIELD_KEYS | {"option_labels"}):
            return _unknown("UNSUPPORTED_FIELD_METADATA")
    normalized_fields = []
    for source in fields:
        field = dict(source)
        option_labels = field.pop("option_labels", None)
        if option_labels is not None:
            if (field.get("type") != "select" or not isinstance(option_labels, list)
                    or len(option_labels) > 100
                    or any(not isinstance(label, str) or len(label) > 500 for label in option_labels)):
                return _unknown("INVALID_OPTIONS_METADATA")
            normalized_labels = [" ".join(label.split()) for label in option_labels]
            field["options_fingerprint"] = hashlib.sha256(
                json.dumps(normalized_labels, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        normalized_fields.append(field)
    repeated = {}
    repeatable_types = {"text", "email", "tel", "url", "number", "date", "select", "textarea"}
    for field in normalized_fields:
        name = field.get("name")
        field_type = field.get("type")
        if isinstance(name, str) and field_type in repeatable_types:
            group = _REPEAT_INDEX.sub("", name)
            repeated.setdefault(group, []).append(field)
    for group, group_fields in repeated.items():
        if len(group_fields) > 1:
            for field in group_fields:
                field["repeatable"] = True
                field["repeatable_group"] = group
    return analyze_workday_form({
        "provider": "workday",
        "schema_version": 1,
        "form_id": "workday-static-form",
        "revision": "static-markup-v1",
        "fields": normalized_fields,
    })


class _WorkdayMarkupParser(HTMLParser):
    """Collect only static Workday control metadata and visible label text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.form_count = 0
        self.active_form = False
        self.markup_events = 0
        self.controls = []
        self.labels_by_id = {}
        self.label_stack = []
        self.ignored_stack = []
        self.value_text_depth = 0
        self.challenge_protected = False
        self.unsupported_interactive = False
        self.outside_form_controls = False
        self.controlled_targets = {}
        self.element_stack = []
        self.invalid = False
        self.option_capture = None

    def _record_event(self):
        if self.invalid:
            return False
        self.markup_events += 1
        if self.markup_events > MAX_WORKDAY_MARKUP_EVENTS:
            self.invalid = True
            return False
        return True

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
        if not self._record_event():
            return
        tag = tag.casefold()
        values = self._attrs(attrs)
        self._check_challenge_metadata(values)
        controlled_by = self.element_stack[-1][1] if self.element_stack else None
        element_id = values.get("id")
        if isinstance(element_id, str) and element_id in self.controlled_targets:
            controlled_by = self.controlled_targets[element_id]
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.element_stack.append((tag, controlled_by))
        if tag == "option" and self.active_form and self.controls and self.controls[-1].get("type") == "select":
            self.option_capture = {"control": len(self.controls) - 1, "parts": []}
            return
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
            if len(self.label_stack) >= MAX_WORKDAY_LABEL_DEPTH:
                self.invalid = True
                return
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
        control_id = values.get("id")
        aria_controls = values.get("aria-controls")
        if isinstance(control_id, str) and isinstance(aria_controls, str):
            for target in aria_controls.split():
                self.controlled_targets[target] = control_id
        self.controls.append({
            "id": control_id,
            "name": values.get("name"),
            "placeholder": values.get("placeholder"),
            "aria_label": values.get("aria-label"),
            "type": input_type.casefold() if isinstance(input_type, str) else "",
            "required": (
                any(str(name).casefold() == "required" for name, _ in attrs)
                or (isinstance(values.get("aria-required"), str)
                    and values["aria-required"].casefold() == "true")
            ),
            "conditional": True if controlled_by else self._semantic_flag(conditional),
            "conditional_on": controlled_by or values.get("data-workday-conditional-on"),
            "repeatable": self._semantic_flag(repeatable),
            "repeatable_group": values.get("data-workday-repeatable-group"),
            "labels": [],
            "option_labels": [] if input_type.casefold() == "select" else None,
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
        if not self._record_event():
            return
        tag = tag.casefold()
        for index in range(len(self.element_stack) - 1, -1, -1):
            if self.element_stack[index][0] == tag:
                del self.element_stack[index:]
                break
        if self.ignored_stack:
            if tag == self.ignored_stack[-1]:
                self.ignored_stack.pop()
            return
        if tag == "option" and self.option_capture is not None:
            capture = self.option_capture
            label = " ".join(" ".join(capture["parts"]).split())
            if len(label) > 500:
                self.invalid = True
            elif capture["control"] < len(self.controls):
                option_labels = self.controls[capture["control"]]["option_labels"]
                if len(option_labels) >= 100:
                    self.invalid = True
                else:
                    option_labels.append(label)
            self.option_capture = None
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
        if not self._record_event() or self.value_text_depth:
            return
        if self.option_capture is not None:
            if sum(map(len, self.option_capture["parts"])) + len(data) > 500:
                self.invalid = True
            else:
                self.option_capture["parts"].append(data)
            return
        if self.ignored_stack:
            return
        for label in self.label_stack:
            if sum(map(len, label["parts"])) + len(data) > 500:
                self.invalid = True
            else:
                label["parts"].append(data)

    def handle_comment(self, data):
        self._record_event()

    def handle_decl(self, decl):
        self._record_event()

    def handle_pi(self, data):
        self._record_event()

    def unknown_decl(self, data):
        self._record_event()

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

        repeatable_names = {}
        repeatable_types = {"text", "email", "tel", "url", "number", "date", "select", "textarea"}
        for control in self.controls:
            name = control.get("name")
            if isinstance(name, str) and control["type"] in repeatable_types:
                group = _REPEAT_INDEX.sub("", name)
                repeatable_names.setdefault(group, []).append(control)
        for group, controls in repeatable_names.items():
            if len(controls) > 1:
                for control in controls:
                    control["repeatable"] = True
                    control["repeatable_group"] = group

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
            field = {
                "id": internal_id,
                "label": label,
                "type": control["type"],
                "required": control["required"],
                "conditional": control["conditional"],
                "conditional_on": control.get("conditional_on"),
                "repeatable": control["repeatable"],
                "repeatable_group": control.get("repeatable_group"),
            }
            if control["type"] == "select":
                labels = [" ".join(value.split()) for value in control["option_labels"]]
                field["options_fingerprint"] = hashlib.sha256(
                    json.dumps(labels, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
            fields.append(field)
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


def validate_workday_form_html(source, expected_analysis: Mapping) -> dict:
    """Reanalyze current static HTML and invalidate a changed question snapshot.

    The comparison uses only the analyzer's value-free field projection. Answer
    values, selected options, and textarea contents are never observed.
    """
    if (not isinstance(expected_analysis, Mapping)
            or expected_analysis.get("provider") != "workday"
            or expected_analysis.get("status") not in {"PASS", "REVIEW_REQUIRED"}
            or not isinstance(expected_analysis.get("fields"), list)
            or not expected_analysis.get("fields")):
        return _unknown("INVALID_EXPECTED_ANALYSIS")
    current = analyze_workday_form_html(source)
    if current.get("status") == "UNKNOWN":
        return current
    if current != expected_analysis:
        return _unknown("FORM_CHANGED")
    return current
