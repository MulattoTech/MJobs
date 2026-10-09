"""Read-only API for profile-scoped reusable-answer suggestions."""
from __future__ import annotations

import re

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy.exc import NoResultFound

from services.documents import answers as answer_service


_SENSITIVE_QUESTION = re.compile(
    rf"(?:{answer_service._SENSITIVE_TERMS.pattern})|"
    r"\b(?:medical\w*|health(?:care)?\s+(?:condition|status|history)|"
    r"diagnos\w*|illness\w*|disease\w*|medication\w*|treatment\w*)\b",
    re.IGNORECASE,
)
_EXPLANATIONS = {
    "EXACT_NORMALIZATION": "The saved question matches after case and punctuation normalization.",
    "CURATED_SEMANTIC": "The saved question matches a curated ordinary paraphrase.",
    "FUZZY": "The saved question meets the configured high-confidence wording threshold.",
}
_MAX_CANDIDATES = 3


def _candidate_matches(question, profile_answers, policy):
    candidates = []
    if not isinstance(profile_answers, (list, tuple)):
        return candidates
    for answer in profile_answers:
        if not isinstance(answer, dict):
            continue
        answer_id = answer.get("id")
        saved_question = answer.get("question")
        answer_text = answer.get("text")
        source = answer.get("source")
        confidence = answer.get("confidence")
        if (not isinstance(answer_id, str) or not answer_id.strip()
                or not isinstance(saved_question, str) or not saved_question.strip()
                or not isinstance(answer_text, str) or not answer_text.strip()
                or not isinstance(source, str) or not source.strip()
                or answer.get("verified") is not True
                or answer.get("user_approved") is not True
                or type(confidence) is not int or not 0 <= confidence <= 100
                or confidence < policy["minimum_saved_answer_confidence"]
                or _SENSITIVE_QUESTION.search(saved_question)):
            continue
        match = answer_service.match_answer_question(
            question,
            saved_question,
            minimum_confidence=policy["minimum_question_match_confidence"],
        )
        if match is None:
            continue
        candidates.append({
            "answer_id": answer_id,
            "question": saved_question,
            "text": answer_text,
            "source": source,
            "confidence": confidence,
            "verified": True,
            "user_approved": True,
            "method": match["method"],
            "score": match["score"],
            "explanation": _EXPLANATIONS[match["method"]],
        })
    candidates.sort(key=lambda candidate: (-candidate["score"], candidate["answer_id"]))
    return candidates[:_MAX_CANDIDATES]


def create_application_answer_matching_router(store) -> APIRouter:
    """Build an advisory matching endpoint; selection and export remain separate."""
    router = APIRouter()

    @router.post("/profiles/{profile_id}/applications/{application_id}/answers/match")
    def match_application_answers(profile_id: str, application_id: str,
                                  payload: object = Body(...)):
        if (not isinstance(payload, dict) or set(payload) != {"question"}
                or not isinstance(payload.get("question"), str)
                or not payload["question"].strip() or len(payload["question"]) > 2000):
            raise HTTPException(422, {"code": "ANSWER_MATCH_INVALID_REQUEST"})
        question = payload["question"].strip()
        if _SENSITIVE_QUESTION.search(question):
            raise HTTPException(422, {"code": "ANSWER_MATCH_SENSITIVE_QUESTION"})

        try:
            profile = store.profile(profile_id)
        except NoResultFound:
            raise HTTPException(
                404, {"code": "ANSWER_MATCH_PROFILE_NOT_FOUND"},
            ) from None
        try:
            application = store.application(application_id)
        except NoResultFound:
            raise HTTPException(
                404, {"code": "ANSWER_MATCH_APPLICATION_NOT_FOUND"},
            ) from None
        if application["profile_id"] != profile_id:
            raise HTTPException(
                404, {"code": "ANSWER_MATCH_APPLICATION_NOT_FOUND"},
            )

        policy = answer_service.get_answer_confidence_policy(store, profile_id)
        candidates = _candidate_matches(question, profile.get("answers", []), policy)
        return {
            "profile_id": profile_id,
            "application_id": application_id,
            "question": question,
            "candidates": candidates,
        }

    return router
