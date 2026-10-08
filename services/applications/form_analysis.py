"""Bounded read-only extraction of application form control metadata."""
from __future__ import annotations

import re


MAX_CONTROLS = 100
MAX_OPTIONS = 100
MAX_METADATA_LENGTH = 500

_CONTROL_TAGS = {"input", "select", "textarea"}
_CONTROL_KEYS = {
    "tag", "label", "labels", "name", "id", "placeholder", "aria_label",
    "input_type", "nearby_text", "section_heading", "fieldset_legend", "options",
    "unsafe_text_context",
}
_FORBIDDEN_KEYS = {
    "value", "current_value", "selected_value", "checked", "secret", "cookie",
    "credential", "password", "token", "answer",
}
_CREDENTIAL_TEXT = re.compile(
    r"\b(password|passcode|credential|api[ _-]?key|token|cookie|secret|private[ _-]?key)\b",
    re.I,
)
_SENSITIVE_TEXT = re.compile(
    r"\b(race|ethnic\w*|gender|sex|sexual orientation|disabilit\w*|veteran\w*|relig\w*|"
    r"date of birth|birth date|social security(?: number)?|ssn|national id|national origin|"
    r"age|marital|citizenship|citizen|work authorization|eligible to work|right to work|"
    r"visa|sponsorship|criminal|convict\w*|felon\w*|salary expectation\w*|"
    r"conflict\w* of interest)\b",
    re.I,
)

_DOM_METADATA_SCRIPT = r"""form => {
  // Rendered semantic text can be static copy or a current answer. Detect only
  // whether it exists; never return its content.
  const hasRenderedText = node => Boolean(node && String(node.textContent || '').trim());
  const controls = Array.from(form.querySelectorAll('input,select,textarea')).slice(0, 101);
  return controls.map(element => {
    const tag = element.tagName.toLowerCase();
    const labelNodes = Array.from(element.labels || []);
    const fieldset = element.closest('fieldset');
    let section = element.parentElement;
    let heading = null;
    while (section && section !== form.parentElement) {
      if (section.matches('section,article,main,form')) {
        heading = section.querySelector('h1,h2,h3,h4,h5,h6');
        if (heading) break;
      }
      section = section.parentElement;
    }
    const legend = fieldset && fieldset.querySelector('legend');
    const unsafeText = labelNodes.some(hasRenderedText) || hasRenderedText(heading) || hasRenderedText(legend);
    return {
      tag,
      labels: [],
      name: element.getAttribute('name'),
      id: element.getAttribute('id'),
      placeholder: element.getAttribute('placeholder'),
      aria_label: element.getAttribute('aria-label'),
      input_type: tag === 'input' ? (element.getAttribute('type') || 'text') : null,
      nearby_text: null,
      section_heading: null,
      fieldset_legend: null,
      unsafe_text_context: unsafeText,
      options: tag === 'select'
        ? Array.from(element.options).slice(0, 101).map(option => option.label)
        : []
    };
  });
}"""


def _unknown(reason: str) -> dict:
    return {"status": "UNKNOWN", "reason": reason, "fields": []}


def _normalize_text(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_METADATA_LENGTH:
        raise ValueError("INVALID_TEXT_METADATA")
    normalized = " ".join(value.split())
    return normalized or None


def _option_labels(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_OPTIONS:
        raise ValueError("INVALID_OPTION_METADATA")
    labels = []
    for value in values:
        if isinstance(value, str):
            label = value
        elif isinstance(value, dict):
            # Option values are deliberately ignored; only their visible labels are metadata.
            label = value.get("label")
        else:
            raise ValueError("INVALID_OPTION_METADATA")
        normalized = _normalize_text(label)
        if normalized and normalized not in labels:
            labels.append(normalized)
    return labels


def _sensitivity(field: dict) -> str:
    if (field.get("input_type") or "").casefold() == "password":
        return "CREDENTIAL"
    parts = [
        *field["labels"], field["name"], field["id"], field["placeholder"],
        field["aria_label"], field["nearby_text"], field["section_heading"],
        field["fieldset_legend"], *field["options"],
    ]
    text = " ".join(part for part in parts if part).casefold()
    if _CREDENTIAL_TEXT.search(text):
        return "CREDENTIAL"
    if _SENSITIVE_TEXT.search(text):
        return "SENSITIVE"
    return "STANDARD"


def analyze_controls(controls) -> dict:
    """Return safe metadata only; malformed or ambiguous inputs fail closed."""
    if not isinstance(controls, list):
        return _unknown("INVALID_CONTROL_LIST")
    if not controls:
        return _unknown("NO_FORM_CONTROLS")
    if len(controls) > MAX_CONTROLS:
        return _unknown("CONTROL_LIMIT_EXCEEDED")

    fields = []
    try:
        for index, control in enumerate(controls):
            if not isinstance(control, dict):
                raise ValueError("INVALID_CONTROL_METADATA")
            keys = {str(key).casefold() for key in control}
            if keys & _FORBIDDEN_KEYS:
                return _unknown("FORBIDDEN_VALUE_METADATA")
            if not keys.issubset(_CONTROL_KEYS):
                raise ValueError("INVALID_CONTROL_METADATA")
            if control.get("unsafe_text_context") is True:
                return _unknown("UNSAFE_TEXT_CONTEXT")
            tag = control.get("tag")
            if not isinstance(tag, str) or tag.casefold() not in _CONTROL_TAGS:
                raise ValueError("INVALID_CONTROL_METADATA")
            labels = control.get("labels")
            if labels is None:
                labels = []
            elif isinstance(labels, str):
                labels = [labels]
            if not isinstance(labels, list) or len(labels) > 20:
                raise ValueError("INVALID_LABEL_METADATA")
            normalized_labels = []
            for label in labels:
                normalized = _normalize_text(label)
                if normalized and normalized not in normalized_labels:
                    normalized_labels.append(normalized)
            singular_label = _normalize_text(control.get("label"))
            if singular_label and singular_label not in normalized_labels:
                normalized_labels.append(singular_label)
            options = _option_labels(control.get("options"))
            field = {
                "index": index,
                "tag": tag.casefold(),
                "labels": normalized_labels,
                "name": _normalize_text(control.get("name")),
                "id": _normalize_text(control.get("id")),
                "placeholder": _normalize_text(control.get("placeholder")),
                "aria_label": _normalize_text(control.get("aria_label")),
                "input_type": _normalize_text(control.get("input_type")),
                "nearby_text": _normalize_text(control.get("nearby_text")),
                "section_heading": _normalize_text(control.get("section_heading")),
                "fieldset_legend": _normalize_text(control.get("fieldset_legend")),
                "options": options,
                "ambiguous": False,
            }
            if not any((field["labels"], field["name"], field["id"], field["placeholder"],
                        field["aria_label"], field["nearby_text"], field["section_heading"],
                        field["fieldset_legend"], field["options"])):
                raise ValueError("UNIDENTIFIABLE_CONTROL")
            field["sensitivity"] = _sensitivity(field)
            fields.append(field)
    except ValueError as error:
        return _unknown(str(error) or "INVALID_CONTROL_METADATA")

    ids = {}
    names = {}
    bare_labels = {}
    for field in fields:
        if field["id"]:
            ids.setdefault(field["id"].casefold(), []).append(field)
        if field["name"]:
            names.setdefault((field["name"].casefold(), field["input_type"] or ""), []).append(field)
        if not field["id"] and not field["name"] and len(field["labels"]) == 1:
            bare_labels.setdefault(field["labels"][0].casefold(), []).append(field)
    ambiguous = False
    for groups in (ids, bare_labels):
        for rows in groups.values():
            if len(rows) > 1:
                ambiguous = True
                for field in rows:
                    field["ambiguous"] = True
    for (_, input_type), rows in names.items():
        if len(rows) > 1 and input_type not in {"radio", "checkbox"}:
            ambiguous = True
            for field in rows:
                field["ambiguous"] = True
    return {
        "status": "UNKNOWN" if ambiguous else "PASS",
        "reason": "AMBIGUOUS_CONTROL_IDENTITY" if ambiguous else None,
        "fields": fields,
    }


def analyze_questionnaire(questionnaire) -> dict:
    """Project mock-question metadata through the same safe control analyzer."""
    if not isinstance(questionnaire, dict) or not isinstance(questionnaire.get("questions"), list):
        return _unknown("INVALID_QUESTIONNAIRE_METADATA")
    questions = questionnaire["questions"]
    if not questions or len(questions) > MAX_CONTROLS:
        return _unknown("QUESTION_LIMIT_EXCEEDED" if len(questions) > MAX_CONTROLS
                        else "NO_QUESTION_METADATA")
    controls = []
    for question in questions:
        if not isinstance(question, dict):
            return _unknown("INVALID_QUESTION_METADATA")
        raw_tag = question.get("tag")
        raw_type = question.get("input_type", question.get("type"))
        tag = raw_tag if isinstance(raw_tag, str) else "input"
        if tag.casefold() not in _CONTROL_TAGS:
            tag = raw_type.casefold() if isinstance(raw_type, str) and raw_type.casefold() in _CONTROL_TAGS else "input"
        options = question.get("options")
        if isinstance(options, list):
            options = [option.get("label") if isinstance(option, dict) else option
                       for option in options]
        controls.append({
            "tag": tag,
            "label": question.get("label"),
            "labels": question.get("labels"),
            "name": question.get("name"),
            "id": question.get("id"),
            "placeholder": question.get("placeholder"),
            "aria_label": question.get("aria_label", question.get("aria-label")),
            "input_type": raw_type,
            "nearby_text": question.get("nearby_text"),
            "section_heading": question.get("section_heading"),
            "fieldset_legend": question.get("fieldset_legend"),
            "options": options,
        })
    return analyze_controls(controls)


def analyze_form_page(page) -> dict:
    """Extract metadata from one already-loaded DOM form without interacting with controls."""
    try:
        forms = page.locator("form")
        form_count = forms.count()
        if form_count != 1:
            return _unknown("NO_FORM" if form_count == 0 else "AMBIGUOUS_FORM")
        controls = forms.first.evaluate(_DOM_METADATA_SCRIPT)
        return analyze_controls(controls)
    except Exception:
        # Do not persist exception text: browser errors can contain page data.
        return _unknown("DOM_ANALYSIS_FAILED")
