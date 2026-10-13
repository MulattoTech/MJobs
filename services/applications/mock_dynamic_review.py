"""Explicit, value-free review of observed conditional MOCK dropdowns.

Observation is not consent. Existing rehearsal receipts bind a human review to
the complete dry-run input identity; no receipt here authorizes submission.
"""
import hashlib
import json
import re

from sqlalchemy import select

from packages.persistence import models as m
from services.applications.mock_answer_bindings import (
    _eligible_fact, bind_mock_answer_slots, normalize_mock_question,
)
from services.applications.rehearsal_store import (
    RehearsalStoreError, acknowledge_application_rehearsal, get_application_rehearsal,
)
from services.applications.run_requests import current_input_snapshot

ACTION = "MOCK_DYNAMIC_FIELDS_OBSERVED"
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _context(store, profile_id, application_id):
    with store.engine.connect() as connection:
        application = connection.execute(select(m.applications).where(
            m.applications.c.id == application_id,
            m.applications.c.profile_id == profile_id)).mappings().one_or_none()
        if application is None:
            raise RehearsalStoreError("APPLICATION_NOT_FOUND")
        if application["target"] != "MOCK":
            raise RehearsalStoreError("MOCK_DYNAMIC_TARGET_UNSUPPORTED")
        store.assert_workspace_mode("FIXTURE")
        snapshot = current_input_snapshot(connection, application, submit=False)
    profile = store.profile(profile_id)
    return application, snapshot, profile


def _fields(profile, preflight, controls):
    """Only exact known, non-sensitive conditional single-select controls qualify."""
    if not isinstance(controls, list) or not 1 <= len(controls) <= 20:
        return None
    mappings = preflight.get("mappings", [])
    bindings = bind_mock_answer_slots(profile.get("answers", []))
    result, seen = [], set()
    for control in controls:
        key = control.get("question_id")
        if not isinstance(key, str) or not _ID.fullmatch(key) or key in seen:
            return None
        seen.add(key)
        matches = [row for row in mappings if row.get("question_id") == key]
        if len(matches) != 1:
            return None
        row = matches[0]
        condition = row.get("condition")
        if (control.get("tag") != "select" or control.get("input_type") != "select-one"
                or control.get("required") is not True or control.get("repeatable") is not False
                or row.get("supported") is not True or row.get("sensitivity") != "STANDARD"
                or row.get("ambiguous") is not False or row.get("type") != "select"
                or row.get("manual_review_required") is not False
                or not isinstance(condition, dict) or set(condition) != {"question_id", "equals"}
                or condition["question_id"] not in bindings
                or bindings[condition["question_id"]].text != condition["equals"]
                or any(row.get(name) for name in ("fact_ids", "profile_paths", "document_kinds", "application_context"))
                or len(row.get("answer_ids", [])) != 1):
            return None
        options = control.get("options")
        if (not isinstance(options, list) or not 1 <= len(options) <= 100
                or any(not isinstance(item, dict) or set(item) != {"label", "value"}
                       or any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 500
                              for k in ("label", "value")) for item in options)
                or len({item["label"] for item in options}) != len(options)
                or len({item["value"] for item in options}) != len(options)
                or [item["label"] for item in options] != row.get("options")
                or control.get("label") != row.get("question")
                or any(not isinstance(control.get(name), str) or not _ID.fullmatch(control[name])
                       for name in ("id", "name"))):
            return None
        result.append({"question_id": key, "label": row["question"],
            "answer_id": row["answer_ids"][0], "condition": condition,
            "control_id": control["id"], "control_name": control["name"], "options": options})
    return result


def observe(store, application_id, preflight, controls):
    application = store.application(application_id)
    profile_id = application["profile_id"]
    application, context, profile = _context(store, profile_id, application_id)
    fields = _fields(profile, preflight, controls)
    payload = {"version": 1, "context": context,
               "metadata_fingerprint": preflight.get("metadata_fingerprint"),
               "fields": fields, "submission_enabled": False}
    with store.engine.begin() as connection:
        store._audit(connection, application_id, ACTION, payload)
    return read(store, profile_id, application_id)


def preview(store, profile_id, application_id):
    application, context, profile = _context(store, profile_id, application_id)
    with store.engine.connect() as connection:
        observation = connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == application_id, m.audit.c.action == ACTION
        ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1)).scalar_one_or_none()
    fields, blockers = [], []
    if observation is None:
        blockers.append("NO_OBSERVED_CONDITIONAL_FIELDS")
    elif (observation.get("context") != context
          or profile["version"] != application["profile_version"]):
        blockers.append("MOCK_DYNAMIC_INPUTS_CHANGED")
    elif not observation.get("fields"):
        blockers.append("MOCK_DYNAMIC_FIELDS_UNSUPPORTED")
    else:
        for field in observation["fields"]:
            answers = [row for row in profile.get("answers", []) if row.get("id") == field["answer_id"]]
            answer = answers[0] if len(answers) == 1 else {}
            fact = _eligible_fact(answer)
            if (fact is None or not isinstance(answer.get("question"), str)
                    or normalize_mock_question(answer["question"]) != normalize_mock_question(field["label"])
                    or fact.text not in [item["label"] for item in field["options"]]):
                blockers.append("MOCK_DYNAMIC_APPROVED_ANSWER_REQUIRED")
                value = None
            else:
                value = fact.text
            fields.append({**field, "value": value})
    fingerprint = _digest({"purpose": "MOCK_DYNAMIC_DRY_RUN_V1", "observation": observation,
                           "current_inputs": context, "current_profile_version": profile["version"],
                           "blockers": blockers})
    return {"profile_id": profile_id, "application_id": application_id,
            "profile_version": profile["version"], "route_id": application["route_id"],
            "fingerprint": fingerprint, "ready": not blockers, "fields": fields,
            "blockers": blockers, "submission_enabled": False}


def read(store, profile_id, application_id):
    result = get_application_rehearsal(store, profile_id, application_id, preview_builder=preview)
    if not result["ready"]:
        result["latest_review"] = None
    return result


def _ready_preview(store, profile_id, application_id):
    result = preview(store, profile_id, application_id)
    if not result["ready"]:
        raise RehearsalStoreError("MOCK_DYNAMIC_REVIEW_UNAVAILABLE")
    return result


def acknowledge(store, profile_id, application_id, request_id, fingerprint):
    return acknowledge_application_rehearsal(store, profile_id, application_id,
        request_id, fingerprint, True, preview_builder=_ready_preview)


def observed_controls(page, controls):
    """Read bounded descriptors only, never values. Unknown identifiers fail closed."""
    result = []
    for control in controls:
        key = control.get("question_id")
        if not isinstance(key, str) or not _ID.fullmatch(key):
            return []
        locator = page.locator('[data-mjobs-question-id="'+key+'"]')
        if locator.count() != 1:
            return []
        descriptor = locator.evaluate("""node => ({
            live: {question_id: node.getAttribute('data-mjobs-question-id'),
              id: node.id || null, name: node.name || null, tag: node.tagName.toLowerCase(),
              input_type: node.type || null,
              required: node.required === true || node.getAttribute('aria-required') === 'true',
              repeatable: Boolean(node.closest('[data-mjobs-repeatable]')),
              repeatable_group: node.closest('[data-mjobs-repeatable]') ?
                (node.closest('[data-mjobs-repeatable]').getAttribute('data-mjobs-repeatable') || 'group') : null,
              conditional: Boolean(node.closest('[data-mjobs-conditional]') || node.hasAttribute('data-mjobs-dynamic-field'))},
            visible: node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden' &&
              getComputedStyle(node).display !== 'none' && !node.disabled,
            label: Array.from(node.labels || []).map(label => label.textContent.trim()).join(' '),
            options: Array.from(node.options || []).filter(option => option.value !== '')
              .slice(0, 101).map(option => ({label: option.disabled ? '' : option.label,
                                           value: option.value}))
        })""")
        live = descriptor.pop("live")
        if not descriptor.pop("visible") or any(control.get(key) != value for key, value in live.items()):
            return []
        result.append({**control, **descriptor})
    return result


def fill_reviewed(store, application_id, preflight, controls, page, check, *, timeout_ms):
    descriptors = observed_controls(page, controls)
    reviewed = observe(store, application_id, preflight, descriptors)
    if not reviewed["ready"] or reviewed["latest_review"] is None:
        return False
    for field in reviewed["fields"]:
        check()
        page.set_default_timeout(timeout_ms())
        locator = page.locator('select[id="'+field["control_id"]+'"][name="'+field["control_name"]+
                               '"][data-mjobs-question-id="'+field["question_id"]+'"]')
        locator.select_option(label=field["value"])
        expected = next(item["value"] for item in field["options"] if item["label"] == field["value"])
        if locator.input_value() != expected:
            return False
    # A selection can mutate another control. Neither that mutation nor a
    # concurrent input edit may inherit the earlier acknowledgement.
    if observed_controls(page, controls) != descriptors:
        return False
    current = read(store, reviewed["profile_id"], application_id)
    return current["ready"] and current["fingerprint"] == reviewed["fingerprint"]
