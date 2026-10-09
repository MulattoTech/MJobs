"""Deterministic, value-free state evaluation for synthetic dynamic forms.

The module accepts a bounded form schema and a caller-owned answer snapshot. It
does not inspect a browser, choose answers, interact with form controls, or submit
anything. Returned fields contain state and choice metadata only; answer values
are never copied into the result.
"""
from __future__ import annotations

import re
from collections.abc import Mapping


SCHEMA_VERSION = 1
MAX_QUESTIONS = 100
MAX_SECTIONS = 32
MAX_REPEATABLE_GROUPS = 20
MAX_ROWS_PER_GROUP = 20
MAX_CHOICES = 100
MAX_TEXT_LENGTH = 500
MAX_DYNAMIC_FIELDS = 300

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_QUESTION_KEYS = {
    "id", "question_id", "label", "required", "kind", "tag", "input_type", "type",
    "options", "condition", "required_when", "options_by", "section_id",
    "optional_section_id", "requirements", "placeholder", "name",
}
_SECTION_KEYS = {"section_id", "question_ids", "condition"}
_GROUP_KEYS = {
    "group_id", "section_id", "fields", "minimum_rows", "maximum_rows", "required",
}
_GROUP_FIELD_KEYS = {"question_id", "id", "label", "required", "kind", "tag", "input_type", "type", "options"}
_CONDITION_KEYS = {"question_id", "equals"}


def _unknown(reason: str) -> dict:
    """Return a stable fail-closed result without preserving supplied values."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "UNKNOWN",
        "reason": reason,
        "complete": False,
        "fields": [],
        "fields_by_id": {},
        "visible_field_ids": [],
        "hidden_field_ids": [],
        "required_field_ids": [],
        "unresolved_required_field_ids": [],
        "newly_visible_field_ids": [],
        "newly_hidden_field_ids": [],
        "newly_required_field_ids": [],
        "dependent_choices": {},
        "optional_sections": {},
        "repeatable_groups": {},
    }


def _identifier(value) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _label_list(value, *, allow_empty=True) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CHOICES:
        raise ValueError("INVALID_CHOICE_SCHEMA")
    result = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("label")
        if not isinstance(item, str) or len(item) > MAX_TEXT_LENGTH:
            raise ValueError("INVALID_CHOICE_SCHEMA")
        item = " ".join(item.split())
        if not item or item in result:
            if not allow_empty:
                raise ValueError("INVALID_CHOICE_SCHEMA")
            continue
        result.append(item)
    return result


def _condition(value, known_ids: set[str]) -> dict | None:
    if value is None:
        return None
    if (not isinstance(value, Mapping) or set(value) != _CONDITION_KEYS
            or not _identifier(value.get("question_id"))
            or value.get("question_id") not in known_ids
            or not isinstance(value.get("equals"), (str, bool, int, float))):
        raise ValueError("INVALID_CONDITION_SCHEMA")
    if isinstance(value.get("equals"), str) and len(value["equals"]) > MAX_TEXT_LENGTH:
        raise ValueError("INVALID_CONDITION_SCHEMA")
    return {"question_id": value["question_id"], "equals": value["equals"]}


def _options_by(value, known_ids: set[str]) -> dict | None:
    if value is None:
        return None
    if (not isinstance(value, Mapping) or set(value) != {"question_id", "choices"}
            or not _identifier(value.get("question_id"))
            or value.get("question_id") not in known_ids
            or not isinstance(value.get("choices"), Mapping)
            or not value["choices"] or len(value["choices"]) > MAX_CHOICES):
        raise ValueError("INVALID_DEPENDENT_CHOICE_SCHEMA")
    choices = {}
    for parent_value, values in value["choices"].items():
        if not isinstance(parent_value, str) or len(parent_value) > MAX_TEXT_LENGTH:
            raise ValueError("INVALID_DEPENDENT_CHOICE_SCHEMA")
        choices[parent_value] = _label_list(values, allow_empty=False)
        if not choices[parent_value]:
            raise ValueError("INVALID_DEPENDENT_CHOICE_SCHEMA")
    return {"question_id": value["question_id"], "choices": choices}


def _kind(question: Mapping) -> str:
    raw = question.get("kind", question.get("tag", question.get("input_type", question.get("type", "text"))))
    if not isinstance(raw, str):
        raise ValueError("INVALID_QUESTION_KIND")
    raw = raw.casefold()
    if raw in {"select", "select-one", "select-multiple"}:
        return "select"
    if raw in {"checkbox", "radio", "boolean"}:
        return "boolean" if raw != "radio" else "choice"
    if raw in {"text", "textarea", "email", "number", "date", "file", "input"}:
        return "text" if raw in {"textarea", "input"} else raw
    raise ValueError("INVALID_QUESTION_KIND")


def _field_definition(value: Mapping, known_ids: set[str], *, allow_conditions: bool) -> dict:
    if set(value) - _QUESTION_KEYS:
        raise ValueError("INVALID_QUESTION_SCHEMA")
    question_id = value.get("question_id", value.get("id"))
    if not _identifier(question_id) or question_id in known_ids:
        raise ValueError("INVALID_QUESTION_ID")
    if type(value.get("required")) is not bool:
        raise ValueError("INVALID_REQUIRED_FLAG")
    kind = _kind(value)
    options = _label_list(value.get("options"))
    if kind == "select" and not options and value.get("options_by") is None:
        raise ValueError("SELECT_WITHOUT_OPTIONS")
    if kind != "select" and (options or value.get("options_by") is not None):
        raise ValueError("CHOICES_ON_NON_SELECT")
    condition = value.get("condition") if allow_conditions else None
    required_when = value.get("required_when") if allow_conditions else None
    section_id = value.get("section_id", value.get("optional_section_id"))
    if section_id is not None and not _identifier(section_id):
        raise ValueError("INVALID_SECTION_ID")
    known_ids.add(question_id)
    return {
        "question_id": question_id,
        "kind": kind,
        "required": value["required"],
        "options": options,
        "condition_source": condition,
        "required_when_source": required_when,
        "options_by_source": value.get("options_by"),
        "section_id": section_id,
    }


def _schema(value) -> dict:
    if not isinstance(value, Mapping) or set(value) - {
            "schema_version", "questions", "optional_sections", "repeatable_groups"}:
        raise ValueError("INVALID_SCHEMA")
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("UNSUPPORTED_SCHEMA_VERSION")
    raw_questions = value.get("questions")
    raw_sections = value.get("optional_sections", [])
    raw_groups = value.get("repeatable_groups", [])
    if (not isinstance(raw_questions, list) or not raw_questions or len(raw_questions) > MAX_QUESTIONS
            or not isinstance(raw_sections, list) or len(raw_sections) > MAX_SECTIONS
            or not isinstance(raw_groups, list) or len(raw_groups) > MAX_REPEATABLE_GROUPS):
        raise ValueError("INVALID_SCHEMA_COLLECTION")

    ids: set[str] = set()
    questions = []
    for raw in raw_questions:
        if not isinstance(raw, Mapping):
            raise ValueError("INVALID_QUESTION_SCHEMA")
        questions.append(_field_definition(raw, ids, allow_conditions=True))

    sections = {}
    for raw in raw_sections:
        if not isinstance(raw, Mapping) or set(raw) - _SECTION_KEYS:
            raise ValueError("INVALID_OPTIONAL_SECTION_SCHEMA")
        section_id = raw.get("section_id")
        question_ids = raw.get("question_ids", [])
        if (not _identifier(section_id) or section_id in sections
                or not isinstance(question_ids, list) or len(question_ids) > MAX_QUESTIONS
                or any(not _identifier(question_id) or question_id not in ids for question_id in question_ids)
                or len(set(question_ids)) != len(question_ids)):
            raise ValueError("INVALID_OPTIONAL_SECTION_SCHEMA")
        sections[section_id] = {"question_ids": question_ids, "condition_source": raw.get("condition")}

    for question in questions:
        section_id = question["section_id"]
        if section_id is not None and (section_id not in sections
                or question["question_id"] not in sections[section_id]["question_ids"]):
            raise ValueError("UNKNOWN_QUESTION_SECTION")
        if question["section_id"] is None and any(
                question["question_id"] in section["question_ids"] for section in sections.values()):
            raise ValueError("QUESTION_SECTION_MISMATCH")

    groups = {}
    for raw in raw_groups:
        if not isinstance(raw, Mapping) or set(raw) - _GROUP_KEYS:
            raise ValueError("INVALID_REPEATABLE_GROUP_SCHEMA")
        group_id = raw.get("group_id")
        raw_fields = raw.get("fields")
        minimum = raw.get("minimum_rows")
        maximum = raw.get("maximum_rows")
        required = raw.get("required", minimum > 0 if type(minimum) is int else False)
        section_id = raw.get("section_id")
        if (not _identifier(group_id) or group_id in groups
                or not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > MAX_QUESTIONS
                or type(minimum) is not int or type(maximum) is not int
                or minimum < 0 or maximum < 1 or minimum > maximum or maximum > MAX_ROWS_PER_GROUP
                or type(required) is not bool or (required and minimum < 1)
                or (section_id is not None and (not _identifier(section_id) or section_id not in sections))):
            raise ValueError("INVALID_REPEATABLE_GROUP_SCHEMA")
        fields = []
        group_ids = set()
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping) or set(raw_field) - _GROUP_FIELD_KEYS:
                raise ValueError("INVALID_REPEATABLE_FIELD_SCHEMA")
            question_id = raw_field.get("question_id", raw_field.get("id"))
            if (not _identifier(question_id) or question_id in group_ids
                    or type(raw_field.get("required")) is not bool):
                raise ValueError("INVALID_REPEATABLE_FIELD_SCHEMA")
            kind = _kind(raw_field)
            options = _label_list(raw_field.get("options"))
            if kind == "select" and not options:
                raise ValueError("SELECT_WITHOUT_OPTIONS")
            fields.append({"question_id": question_id, "kind": kind,
                           "required": raw_field["required"], "options": options})
            group_ids.add(question_id)
        groups[group_id] = {
            "fields": fields, "minimum_rows": minimum, "maximum_rows": maximum,
            "required": required, "section_id": section_id,
        }

    for question in questions:
        question["condition"] = _condition(question.pop("condition_source"), ids)
        question["required_when"] = _condition(question.pop("required_when_source"), ids)
        question["options_by"] = _options_by(question.pop("options_by_source"), ids)
    for section in sections.values():
        section["condition"] = _condition(section.pop("condition_source"), ids)

    _ensure_acyclic(questions, sections)
    return {"questions": questions, "sections": sections, "groups": groups}


def _ensure_acyclic(questions: list[dict], sections: Mapping[str, dict]) -> None:
    edges = {f"question:{question['question_id']}": [] for question in questions}
    edges.update({f"section:{section_id}": [] for section_id in sections})
    for question in questions:
        node = f"question:{question['question_id']}"
        dependencies = []
        if question["condition"]:
            dependencies.append(f"question:{question['condition']['question_id']}")
        if question["required_when"]:
            dependencies.append(f"question:{question['required_when']['question_id']}")
        if question["options_by"]:
            dependencies.append(f"question:{question['options_by']['question_id']}")
        if question["section_id"]:
            dependencies.append(f"section:{question['section_id']}")
        edges[node] = dependencies
    for section_id, section in sections.items():
        condition = section["condition"]
        edges[f"section:{section_id}"] = (
            [f"question:{condition['question_id']}"] if condition else [])
    visiting = set()
    visited = set()

    def visit(node):
        if node in visiting:
            raise ValueError("DEPENDENCY_CYCLE")
        if node in visited:
            return
        visiting.add(node)
        for dependency in edges.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


def _answer_present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _answers(value, known_ids: set[str]) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("INVALID_ANSWER_STATE")
    if set(value) - known_ids:
        raise ValueError("UNKNOWN_ANSWER_REFERENCE")
    for answer in value.values():
        if isinstance(answer, str) and len(answer) > MAX_TEXT_LENGTH:
            raise ValueError("INVALID_ANSWER_STATE")
        if not (answer is None or isinstance(answer, (str, bool, int, float))):
            raise ValueError("INVALID_ANSWER_STATE")
        if isinstance(answer, float) and (answer != answer or abs(answer) == float("inf")):
            raise ValueError("INVALID_ANSWER_STATE")
    return dict(value)


def _section_expansion(value, sections: Mapping, groups: Mapping) -> set[str]:
    if value is None:
        return set()
    if (not isinstance(value, (list, tuple, set))
            or any(not _identifier(section_id) for section_id in value)):
        raise ValueError("INVALID_OPTIONAL_SECTION_STATE")
    expanded = set(value)
    if expanded - set(sections):
        raise ValueError("UNKNOWN_OPTIONAL_SECTION")
    return expanded


def _repeatable_rows(value, groups: Mapping) -> dict[str, list[str]]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value) - set(groups):
        raise ValueError("UNKNOWN_REPEATABLE_GROUP")
    rows_by_group = {}
    for group_id, group in groups.items():
        rows = value.get(group_id, [])
        if (not isinstance(rows, (list, tuple)) or len(rows) > group["maximum_rows"]
                or any(not _identifier(row_id) for row_id in rows)
                or len(set(rows)) != len(rows)):
            raise ValueError("INVALID_REPEATABLE_ROW_STATE")
        rows_by_group[group_id] = list(rows)
    return rows_by_group


def _matches(condition: dict | None, answers: Mapping) -> bool:
    if condition is None:
        return True
    value = answers.get(condition["question_id"])
    return _answer_present(value) and value == condition["equals"]


def _state_ids(previous_state, key: str) -> set[str]:
    if previous_state is None:
        return set()
    if not isinstance(previous_state, Mapping) or previous_state.get("status") != "PASS":
        raise ValueError("INVALID_PREVIOUS_STATE")
    values = previous_state.get(key)
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError("INVALID_PREVIOUS_STATE")
    return set(values)


def reconcile_dynamic_form_state(form_schema, answers, *, expanded_sections=(),
                                 repeatable_rows=None, previous_state=None) -> dict:
    """Recompute a synthetic form view after each explicit answer/section/row change.

    Schema questions may use the existing mock ATS `id`/`tag`/`condition` fields
    or the equivalent `question_id`/`kind` names. Conditions are exact equals
    comparisons. Optional sections expand only when the caller lists them in
    ``expanded_sections``. Repeatable row IDs and answer values always come from
    the caller; this function never creates a row or chooses a value.
    """
    try:
        schema = _schema(form_schema)
        question_ids = {question["question_id"] for question in schema["questions"]}
        rows_by_group = _repeatable_rows(repeatable_rows, schema["groups"])
        active_ids = set(question_ids)
        for group_id, group in schema["groups"].items():
            for row_id in rows_by_group[group_id]:
                active_ids.update(
                    f"{group_id}[{row_id}].{field['question_id']}" for field in group["fields"])
        answer_values = _answers(answers, active_ids)
        expanded = _section_expansion(expanded_sections, schema["sections"], schema["groups"])

        question_by_id = {question["question_id"]: question for question in schema["questions"]}
        section_visibility = {}
        question_visibility = {}

        def condition_matches(condition):
            if condition is None:
                return True
            parent_id = condition["question_id"]
            value = answer_values.get(parent_id)
            return (question_visible(parent_id) and _answer_present(value)
                    and value == condition["equals"])

        def section_visible(section_id):
            if section_id not in section_visibility:
                section_visibility[section_id] = condition_matches(
                    schema["sections"][section_id]["condition"])
            return section_visibility[section_id]

        def question_visible(question_id):
            if question_id not in question_visibility:
                question = question_by_id[question_id]
                condition_visible = condition_matches(question["condition"])
                section_id = question["section_id"]
                section_is_visible = (
                    section_id is None
                    or (section_visible(section_id) and section_id in expanded)
                )
                question_visibility[question_id] = condition_visible and section_is_visible
            return question_visibility[question_id]

        for section_id in schema["sections"]:
            section_visible(section_id)
        section_rows = {}
        for section_id, section in schema["sections"].items():
            section_rows[section_id] = {
                "visible": section_visibility[section_id],
                "expanded": section_visibility[section_id] and section_id in expanded,
                "question_ids": list(section["question_ids"]),
            }

        fields = []
        dependent_choices = {}
        required_ids = []
        unresolved_ids = []
        for question in schema["questions"]:
            question_id = question["question_id"]
            condition_visible = condition_matches(question["condition"])
            section_id = question["section_id"]
            section_visible = (section_id is None or
                               section_rows[section_id]["visible"] and section_rows[section_id]["expanded"])
            visible = condition_visible and section_visible
            parent_choices = None
            if question["options_by"]:
                parent_id = question["options_by"]["question_id"]
                parent = answer_values.get(parent_id)
                parent_choices = question["options_by"]["choices"].get(parent, [])
                if not question_visible(parent_id):
                    parent_choices = []
                if not visible:
                    parent_choices = []
                dependent_choices[question_id] = list(parent_choices)
            choices = parent_choices if parent_choices is not None else question["options"]
            required = question["required"] or (
                question["required_when"] is not None
                and condition_matches(question["required_when"]))
            required = required and visible
            value = answer_values.get(question_id)
            present = _answer_present(value)
            reason = None
            answer_valid = True
            if present and question["kind"] == "select":
                if question["options_by"] and not choices:
                    answer_valid = False
                    reason = "DEPENDENT_CHOICES_UNAVAILABLE"
                elif value not in choices:
                    answer_valid = False
                    reason = "ANSWER_NOT_IN_CURRENT_CHOICES"
            if visible and required:
                required_ids.append(question_id)
                if not present:
                    reason = "REQUIRED_ANSWER_MISSING"
                    unresolved_ids.append(question_id)
                elif not answer_valid:
                    unresolved_ids.append(question_id)
            fields.append({
                "field_id": question_id,
                "kind": question["kind"],
                "visible": visible,
                "required": required,
                "answered": present,
                "answer_valid": answer_valid,
                "reason": reason,
                "options": list(choices),
            })

        groups_result = {}
        dynamic_field_count = len(fields)
        for group_id, group in schema["groups"].items():
            section_id = group["section_id"]
            visible = section_id is None or section_rows[section_id]["visible"] and section_rows[section_id]["expanded"]
            row_ids = rows_by_group[group_id] if visible else []
            field_ids_by_row = {}
            group_field_ids = []
            for row_id in row_ids:
                row_fields = []
                for field in group["fields"]:
                    field_id = f"{group_id}[{row_id}].{field['question_id']}"
                    row_fields.append(field_id)
                    group_field_ids.append(field_id)
                    dynamic_field_count += 1
                    value = answer_values.get(field_id)
                    present = _answer_present(value)
                    required = field["required"]
                    reason = "REQUIRED_ANSWER_MISSING" if required and not present else None
                    fields.append({
                        "field_id": field_id,
                        "kind": field["kind"],
                        "visible": True,
                        "required": required,
                        "answered": present,
                        "answer_valid": True,
                        "reason": reason,
                        "options": list(field["options"]),
                    })
                    if required:
                        required_ids.append(field_id)
                        if not present:
                            unresolved_ids.append(field_id)
                field_ids_by_row[row_id] = row_fields
            min_row_marker = f"{group_id}.__minimum_rows__"
            if visible and len(rows_by_group[group_id]) < group["minimum_rows"] and group["required"]:
                required_ids.append(min_row_marker)
                unresolved_ids.append(min_row_marker)
            groups_result[group_id] = {
                "visible": visible,
                "row_ids": list(row_ids),
                "field_ids": group_field_ids,
                "field_ids_by_row": field_ids_by_row,
                "minimum_rows": group["minimum_rows"],
                "maximum_rows": group["maximum_rows"],
                "rows_required": group["required"],
                "minimum_rows_satisfied": len(rows_by_group[group_id]) >= group["minimum_rows"] if visible else True,
            }
        if dynamic_field_count > MAX_DYNAMIC_FIELDS:
            raise ValueError("DYNAMIC_FIELD_LIMIT_EXCEEDED")

        visible_field_ids = [field["field_id"] for field in fields if field["visible"]]
        hidden_field_ids = [field["field_id"] for field in fields if not field["visible"]]
        previous_visible = _state_ids(previous_state, "visible_field_ids")
        previous_required = _state_ids(previous_state, "required_field_ids")
        current_visible = set(visible_field_ids)
        current_required = set(required_ids)
        newly_visible = [field_id for field_id in visible_field_ids if field_id not in previous_visible]
        newly_hidden = [field_id for field_id in previous_state.get("visible_field_ids", [])
                        if field_id not in current_visible] if previous_state else []
        newly_required = [field_id for field_id in required_ids if field_id not in previous_required]
        fields_by_id = {field["field_id"]: dict(field) for field in fields}
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS",
            "reason": None,
            "complete": not unresolved_ids,
            "fields": fields,
            "fields_by_id": fields_by_id,
            "visible_field_ids": visible_field_ids,
            "hidden_field_ids": hidden_field_ids,
            "required_field_ids": required_ids,
            "unresolved_required_field_ids": unresolved_ids,
            "newly_visible_field_ids": newly_visible,
            "newly_hidden_field_ids": newly_hidden,
            "newly_required_field_ids": newly_required,
            "dependent_choices": dependent_choices,
            "optional_sections": section_rows,
            "repeatable_groups": groups_result,
        }
    except ValueError as error:
        return _unknown(str(error) or "INVALID_DYNAMIC_FORM_STATE")
