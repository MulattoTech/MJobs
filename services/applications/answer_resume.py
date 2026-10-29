"""Exact reviewed-answer binding for a non-submitting mock application resume."""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm.exc import NoResultFound

from packages.persistence import models as m
from packages.persistence.store import identity
from services.applications.attempt_history import AttemptConflict
from services.applications.preflight import latest_preflight
from services.applications.run_requests import queue_mock_run, read_mock_run


_HASH = re.compile(r"[0-9a-f]{64}")
def _normalized(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def question_fingerprint(profile_id, application_id, question_id, question):
    payload = json.dumps({
        "profile_id": profile_id,
        "application_id": application_id,
        "question_id": question_id,
        "question": _normalized(question),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _scoped_application(store, profile_id, application_id):
    application = store.application(application_id)
    if application["profile_id"] != profile_id:
        raise NoResultFound("APPLICATION_NOT_FOUND")
    if application["target"] != "MOCK":
        raise AttemptConflict("APPLICATION_RUN_TARGET_UNSUPPORTED")
    return application


def _question(preflight, question_id):
    if not isinstance(preflight, dict):
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_UNAVAILABLE")
    matches = [row for group in ("missing", "mappings")
               for row in preflight.get(group, [])
               if row.get("question_id") == question_id]
    questions = {_normalized(row.get("question")) for row in matches
                 if _normalized(row.get("question"))}
    if len(questions) != 1:
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_UNAVAILABLE")
    return next(iter(questions))


def _answer(profile, answer_id, question):
    matches = [row for row in profile.get("answers", [])
               if row.get("id") == answer_id]
    if len(matches) != 1:
        raise AttemptConflict("APPLICATION_ANSWER_NOT_APPROVED")
    answer = matches[0]
    if (answer.get("user_approved") is not True or not answer.get("text", "").strip()
            or _normalized(answer.get("question")) != _normalized(question)):
        raise AttemptConflict("APPLICATION_ANSWER_NOT_APPROVED")
    return answer


def _public_answer(answer):
    return {key: answer.get(key) for key in (
        "id", "question", "text", "user_approved", "verified", "confidence", "source",
    )}


def read_review(store, profile_id, application_id, question_id):
    application = _scoped_application(store, profile_id, application_id)
    profile = store.profile(profile_id)
    preflight = latest_preflight(store, application_id)
    if not preflight or preflight.get("is_current") is not True:
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_STALE")
    form_fingerprint = preflight.get("metadata_fingerprint")
    if not isinstance(form_fingerprint, str) or not _HASH.fullmatch(form_fingerprint):
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_UNAVAILABLE")
    question = _question(preflight, question_id)
    return {
        "profile_id": profile_id,
        "application_id": application_id,
        "profile_version": profile["version"],
        "form_fingerprint": form_fingerprint,
        "question": {
            "question_id": question_id,
            "fingerprint": question_fingerprint(
                profile_id, application_id, question_id, question,
            ),
            "text": question,
            "reason": preflight.get("reason") or "Required answer needs review.",
            "supported": True,
        },
        "answer_bank": [_public_answer(row) for row in profile.get("answers", [])],
    }


def _binding(store, profile_id, application_id, value):
    application = _scoped_application(store, profile_id, application_id)
    profile = store.profile(profile_id)
    if (profile["version"] != value["expected_profile_version"]
            or application["profile_version"] != value["expected_profile_version"]):
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_STALE")
    preflight = latest_preflight(store, application_id)
    form_fingerprint = preflight.get("metadata_fingerprint") if preflight else None
    question = _question(preflight, value["question_id"])
    if (form_fingerprint != value["form_fingerprint"]
            or question_fingerprint(
                profile_id, application_id, value["question_id"], question,
            )
            != value["question_fingerprint"]):
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_STALE")
    answer = _answer(profile, value["answer_id"], question)
    return {
        "profile_id": profile_id,
        "application_id": application_id,
        "question_id": value["question_id"],
        "question": question,
        "question_fingerprint": value["question_fingerprint"],
        "form_fingerprint": value["form_fingerprint"],
        "answer_id": answer["id"],
        "expected_profile_version": value["expected_profile_version"],
        "preflight_event_id": preflight.get("event_id"),
    }


def admit_resume(store, profile_id, application_id, value):
    binding = _binding(store, profile_id, application_id, value)
    return queue_mock_run(
        store, profile_id, application_id, submit=False,
        request_id=value["request_id"], resume_binding=binding,
    )


def validate_resume_binding(store, operation):
    binding = operation.get("params", {}).get("resume_binding")
    if binding is None:
        return
    current = _binding(store, binding["profile_id"], binding["application_id"], {
        "question_id": binding["question_id"],
        "question_fingerprint": binding["question_fingerprint"],
        "form_fingerprint": binding["form_fingerprint"],
        "answer_id": binding["answer_id"],
        "expected_profile_version": binding["expected_profile_version"],
    })
    if current != binding:
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_STALE")


def complete_resume_result(store, connection, operation):
    """Validate and build the immutable result inside the operation completion fence."""
    params = operation.get("params", {})
    binding = params.get("resume_binding")
    if binding is None:
        return None
    application = connection.execute(select(m.applications).where(
        m.applications.c.id == binding["application_id"],
        m.applications.c.profile_id == binding["profile_id"],
    ).with_for_update()).mappings().one_or_none()
    if application is None:
        raise NoResultFound("APPLICATION_NOT_FOUND")
    if application["target"] != "MOCK":
        raise AttemptConflict("APPLICATION_RUN_TARGET_UNSUPPORTED")
    profile_head = connection.execute(select(m.profiles).where(
        m.profiles.c.id == binding["profile_id"],
    ).with_for_update()).mappings().one_or_none()
    if profile_head is None:
        raise NoResultFound("PROFILE_NOT_FOUND")
    profile = store.profile(binding["profile_id"], connection=connection)
    answer = _answer(profile, binding["answer_id"], binding["question"])
    preflight_row = connection.execute(select(m.audit).where(
        m.audit.c.entity_id == binding["application_id"],
        m.audit.c.action == "APPLICATION_QUESTION_PREFLIGHT",
    ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc()).limit(1).with_for_update()
    ).mappings().one_or_none()
    preflight = None if preflight_row is None else {
        **preflight_row["payload"],
        "event_id": preflight_row["id"],
        "is_current": bool(
            profile_head["version"] == application["profile_version"]
            and preflight_row["payload"].get("profile_version") == application["profile_version"]
            and preflight_row["payload"].get("document_version") == application["document_version"]
            and preflight_row["payload"].get("route_id") == application["route_id"]
        ),
    }
    if (profile_head["version"] != binding["expected_profile_version"]
            or application["profile_version"] != binding["expected_profile_version"]):
        raise AttemptConflict("APPLICATION_ANSWER_REVIEW_STALE")
    if (application["state"] != "DRY_RUN_COMPLETE" or not preflight
            or preflight.get("result") != "PASS" or preflight.get("is_current") is not True):
        raise AttemptConflict("APPLICATION_RESUME_PREFLIGHT_FAILED")
    if preflight.get("event_id") == binding.get("preflight_event_id"):
        raise AttemptConflict("APPLICATION_RESUME_PREFLIGHT_STALE")
    if preflight.get("metadata_fingerprint") != binding["form_fingerprint"]:
        raise AttemptConflict("APPLICATION_RESUME_PREFLIGHT_STALE")
    question = _question(preflight, binding["question_id"])
    if (question_fingerprint(
            binding["profile_id"], binding["application_id"],
            binding["question_id"], question)
            != binding["question_fingerprint"]):
        raise AttemptConflict("APPLICATION_RESUME_PREFLIGHT_STALE")
    return {
        "request_id": params.get("request_id", ""),
        "profile_id": binding["profile_id"],
        "application_id": binding["application_id"],
        "question_id": binding["question_id"],
        "question_fingerprint": binding["question_fingerprint"],
        "form_fingerprint": binding["form_fingerprint"],
        "profile_version": binding["expected_profile_version"],
        "external_submission": "OFF",
        "answer": _public_answer(answer),
        "preflight": {"result": "PASS", "is_current": True},
    }


def _stored_result(operation, binding, request_id):
    result = operation["params"].get("resume_result")
    if (not isinstance(result, dict)
            or result.get("request_id") != request_id
            or result.get("profile_id") != binding["profile_id"]
            or result.get("application_id") != binding["application_id"]
            or result.get("question_id") != binding["question_id"]
            or result.get("question_fingerprint") != binding["question_fingerprint"]
            or result.get("form_fingerprint") != binding["form_fingerprint"]
            or result.get("profile_version") != binding["expected_profile_version"]
            or result.get("external_submission") != "OFF"
            or result.get("preflight") != {"result": "PASS", "is_current": True}
            or not isinstance(result.get("answer"), dict)
            or result["answer"].get("id") != binding["answer_id"]
            or result["answer"].get("user_approved") is not True):
        raise AttemptConflict("APPLICATION_RESUME_RECEIPT_MISSING")
    return result


def read_resume(store, profile_id, application_id, request_id):
    receipt = read_mock_run(store, profile_id, application_id, request_id)
    if receipt["state"] in {"QUEUED", "RUNNING"}:
        return None, receipt
    operation_id = identity(
        "application-run-request-v1", profile_id, application_id, request_id,
    )
    with store.engine.connect() as connection:
        operation = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id,
            m.operations.c.entity_id == application_id,
            m.operations.c.kind == "APPLY_MOCK",
        )).mappings().one_or_none()
    if operation is None:
        raise NoResultFound("APPLICATION_RUN_NOT_FOUND")
    binding = operation["params"].get("resume_binding")
    if not isinstance(binding, dict):
        raise AttemptConflict("APPLICATION_RESUME_BINDING_MISSING")
    if operation["state"] != "COMPLETED":
        code = operation.get("error")
        raise AttemptConflict(code if isinstance(code, str) and code else
                              "APPLICATION_RESUME_PREFLIGHT_FAILED")
    return _stored_result(operation, binding, request_id), receipt
