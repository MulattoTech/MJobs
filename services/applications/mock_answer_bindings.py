"""Exact, bounded bindings from reviewed profile answers to mock ATS slots."""
from __future__ import annotations

import math
import re

from packages.domain.safety import Fact


MOCK_ANSWER_SLOTS = {
    "country": "Country",
    "remote": "Remote role",
    "fixture_question": "Approved fixture answer",
}
_ANSWER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def normalize_mock_question(value: str) -> str:
    """Normalize punctuation and case only; do not apply semantic or fuzzy matching."""
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _matches_slot(answer: dict, slot_id: str, label: str) -> bool:
    question = answer.get("question")
    if isinstance(question, str) and question.strip():
        return normalize_mock_question(question) == normalize_mock_question(label)
    return answer.get("id") == slot_id and question is None


def _eligible_fact(answer: dict) -> Fact | None:
    if (not isinstance(answer.get("id"), str) or not _ANSWER_ID.fullmatch(answer["id"])
            or not isinstance(answer.get("text"), str) or not answer["text"].strip()
            or not isinstance(answer.get("source"), str) or not answer["source"].strip()
            or answer.get("verified") is not True
            or answer.get("user_approved") is not True
            or type(answer.get("confidence")) not in (int, float)
            or not math.isfinite(answer["confidence"])
            or answer["confidence"] < 80):
        return None
    try:
        return Fact(**{key: answer[key] for key in
                       ("id", "text", "source", "verified", "user_approved", "confidence")})
    except (KeyError, TypeError, ValueError):
        return None


def bind_mock_answer_slots(answers) -> dict[str, Fact]:
    """Return only unique, reviewed exact-question answers for supported mock slots.

    Legacy fixed IDs remain usable only when they have no saved question. A saved
    different question always defeats that ID fallback. Ambiguous or ineligible
    candidates produce no binding, so callers must leave that question unresolved.
    """
    if not isinstance(answers, (list, tuple)):
        return {}
    bindings = {}
    for slot_id, label in MOCK_ANSWER_SLOTS.items():
        candidates = [answer for answer in answers
                      if isinstance(answer, dict) and _matches_slot(answer, slot_id, label)]
        if len(candidates) != 1:
            continue
        fact = _eligible_fact(candidates[0])
        if fact is not None:
            bindings[slot_id] = fact
    counts = {}
    for fact in bindings.values():
        counts[fact.id] = counts.get(fact.id, 0) + 1
    bindings = {slot_id: fact for slot_id, fact in bindings.items()
                if counts[fact.id] == 1}
    return bindings


def preflight_profile_answers(profile: dict, bindings: dict[str, Fact]) -> dict:
    """Exclude invalid slot candidates and keep unique accepted values in preflight input."""
    answers = profile.get("answers", [])
    slot_labels = {normalize_mock_question(label) for label in MOCK_ANSWER_SLOTS.values()}
    slot_ids = set(MOCK_ANSWER_SLOTS)
    kept = []
    for answer in answers if isinstance(answers, list) else []:
        if not isinstance(answer, dict):
            continue
        question = answer.get("question")
        belongs_to_slot = (answer.get("id") in slot_ids
                           or (isinstance(question, str)
                               and normalize_mock_question(question) in slot_labels))
        if not belongs_to_slot:
            kept.append(answer)
    kept.extend({
        "id": fact.id, "text": fact.text, "source": fact.source,
        "verified": fact.verified, "user_approved": fact.user_approved,
        "confidence": fact.confidence,
    } for fact in bindings.values())
    return {**profile, "answers": kept}
