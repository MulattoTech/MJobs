"""Deterministic sensitive-question classification and response proposals.

This module only returns a review-required proposal. Callers remain responsible
for presenting the proposal for application-specific review; it never exports,
autofills, submits, or stores an answer.
"""
from __future__ import annotations

import re


ALWAYS_REQUIRE_REVIEW = "ALWAYS_REQUIRE_REVIEW"
PREFER_NOT_TO_ANSWER = "PREFER_NOT_TO_ANSWER"
USE_CONFIGURED_RESPONSE = "USE_CONFIGURED_RESPONSE"

SENSITIVE_CATEGORIES = frozenset({
    "race_ethnicity",
    "religion",
    "sex_gender",
    "disability",
    "veteran_status",
    "other_voluntary_demographics",
})

PREFER_NOT_TO_ANSWER_TEXT = "Prefer not to answer"
PROFILE_POLICY_SOURCE = "PROFILE_CONFIGURATION"
MAX_QUESTION_LENGTH = 4000
MAX_CONFIGURED_RESPONSE_LENGTH = 2000

_CATEGORY_PATTERNS = (
    ("race_ethnicity", re.compile(
        r"\b(?:race|racial|ethnic(?:ity| origin| background)?)\b", re.IGNORECASE)),
    ("religion", re.compile(
        r"\b(?:religion|religious affiliation|faith tradition)\b", re.IGNORECASE)),
    ("sex_gender", re.compile(
        r"\b(?:sex|gender(?: identity)?|sexual orientation)\b", re.IGNORECASE)),
    ("disability", re.compile(
        r"\b(?:disabilit\w*|disabled)\b", re.IGNORECASE)),
    ("veteran_status", re.compile(
        r"\b(?:veteran|military service|served in the armed forces)\b", re.IGNORECASE)),
    ("other_voluntary_demographics", re.compile(
        r"\b(?:age|date of birth|birth date|marital status|national origin|"
        r"voluntary demographics?|demographic information)\b", re.IGNORECASE)),
)


def classify_sensitive_category(question: object) -> str | None:
    """Classify recognized sensitive demographic wording; unknown text stays unknown."""
    if not isinstance(question, str) or len(question) > MAX_QUESTION_LENGTH:
        return None
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(question):
            return category
    return None


def _review_required(response: str = "") -> dict[str, object]:
    return {
        "response": response,
        "mode": ALWAYS_REQUIRE_REVIEW,
        "review_required": True,
    }


def _valid_configured_response(response: object) -> bool:
    if not isinstance(response, str) or len(response) > MAX_CONFIGURED_RESPONSE_LENGTH:
        return False
    if not response.strip():
        return False
    return not any(
        ord(char) < 32 and char not in "\n\r\t"
        for char in response
    )


def resolve_sensitive_answer(
    category: object,
    sensitive_answer_policies: object,
) -> dict[str, object]:
    """Return a profile-configured, review-required proposal for one known category.

    The accepted policy shape is ``{category: {mode, response?}}``. Missing,
    malformed, and unknown policies resolve to a blank review-required result.
    """
    if not isinstance(category, str) or category not in SENSITIVE_CATEGORIES:
        return _review_required()
    if not isinstance(sensitive_answer_policies, dict):
        return _review_required()

    policy = sensitive_answer_policies.get(category)
    if not isinstance(policy, dict):
        return _review_required()
    if not set(policy).issubset({"mode", "response"}) or "mode" not in policy:
        return _review_required()

    mode = policy["mode"]
    if mode == PREFER_NOT_TO_ANSWER and set(policy) == {"mode"}:
        return {
            "response": PREFER_NOT_TO_ANSWER_TEXT,
            "mode": PREFER_NOT_TO_ANSWER,
            "source": PROFILE_POLICY_SOURCE,
            "review_required": True,
        }
    if mode == USE_CONFIGURED_RESPONSE and set(policy) == {"mode", "response"}:
        response = policy["response"]
        if _valid_configured_response(response):
            return {
                "response": response,
                "mode": USE_CONFIGURED_RESPONSE,
                "source": PROFILE_POLICY_SOURCE,
                "review_required": True,
            }
    if mode == ALWAYS_REQUIRE_REVIEW and set(policy) == {"mode"}:
        return _review_required()
    return _review_required()
