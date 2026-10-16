"""Pure, fail-closed classification for Workday authentication and answer gates.

This module does not interact with a browser, retain session state, or accept
credentials. Callers must report an explicit user-controlled authentication
completion before a previously blocked session can be classified as resumed.
"""
from __future__ import annotations


def classify_workday_gate(
    *,
    authentication_required: bool = False,
    authentication_was_required: bool = False,
    user_completed_authentication: bool = False,
    answer_known: bool = False,
    sensitive: bool = False,
    question_changed: bool = False,
    question_supported: bool = True,
) -> dict[str, str]:
    """Return a fixed gate status and reason without inferring answers.

    Precedence is authentication, changed/unsupported question, sensitive or
    unknown answer, then explicit user resume. ``user_completed_authentication``
    is a trusted caller attestation, not an instruction to automate a login.
    ``READY_FOR_REVIEW`` permits safe analysis to continue and never authorizes
    form filling or submission. Every flag must be an actual bool; malformed
    state holds for human review.
    """
    flags = (
        authentication_required,
        authentication_was_required,
        user_completed_authentication,
        answer_known,
        sensitive,
        question_changed,
        question_supported,
    )
    if any(type(flag) is not bool for flag in flags):
        return {"status": "HUMAN_REVIEW_REQUIRED", "reason": "INVALID_GATE_STATE"}

    if authentication_required:
        return {"status": "AUTHENTICATION_REQUIRED", "reason": "USER_AUTHENTICATION_REQUIRED"}
    if question_changed:
        return {"status": "HUMAN_REVIEW_REQUIRED", "reason": "QUESTION_CHANGED"}
    if not question_supported:
        return {"status": "HUMAN_REVIEW_REQUIRED", "reason": "QUESTION_UNSUPPORTED"}
    if sensitive:
        return {"status": "HUMAN_REVIEW_REQUIRED", "reason": "SENSITIVE_ANSWER"}
    if not answer_known:
        return {"status": "HUMAN_REVIEW_REQUIRED", "reason": "UNKNOWN_ANSWER"}
    if authentication_was_required:
        if not user_completed_authentication:
            return {"status": "HUMAN_REVIEW_REQUIRED", "reason": "USER_RESUME_UNCONFIRMED"}
        return {"status": "SESSION_RESUMED", "reason": "USER_AUTHENTICATION_COMPLETED"}
    return {"status": "READY_FOR_REVIEW", "reason": "SUPPORTED_KNOWN_ANSWER"}
