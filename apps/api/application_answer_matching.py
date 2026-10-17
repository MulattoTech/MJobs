"""Read-only API for profile-scoped reusable-answer suggestions."""
from __future__ import annotations

import math
import re

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy.exc import NoResultFound

from services.answers.review import assess_answer_review, is_review_sensitive_question
from services.career_profile.story_bank import StoryBankError, list_stories
from services.documents import answers as answer_service
from services.documents.tailor import target_fingerprint


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
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 100
                or not math.isfinite(confidence)
                or confidence < policy["minimum_saved_answer_confidence"]
                or _SENSITIVE_QUESTION.search(saved_question)
                or is_review_sensitive_question(saved_question)):
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


def _application_job(store, profile_id, application):
    job_id = application.get("job_id") if isinstance(application, dict) else None
    if not isinstance(job_id, str) or not job_id.strip():
        raise HTTPException(404, {"code": "ANSWER_MATCH_JOB_NOT_FOUND"})
    try:
        return store.job(job_id, profile_id)
    except (NoResultFound, KeyError, TypeError, ValueError):
        raise HTTPException(404, {"code": "ANSWER_MATCH_JOB_NOT_FOUND"}) from None


def _story_reference_rows(store, profile_id):
    try:
        revisions = list_stories(store, profile_id)
    except StoryBankError:
        raise HTTPException(
            503, {"code": "ANSWER_MATCH_STORY_REFERENCES_UNAVAILABLE"},
        ) from None

    references = []
    for story in revisions:
        if not isinstance(story, dict):
            continue
        story_id = story.get("story_id")
        revision = story.get("revision")
        title = story.get("title")
        text = story.get("text")
        fingerprints = story.get("fact_fingerprints")
        fact_ids = story.get("fact_ids")
        allowed_uses = story.get("allowed_uses")
        if (not isinstance(story_id, str) or not story_id.strip()
                or type(revision) is not int or revision < 1
                or not isinstance(title, str) or not title.strip()
                or not isinstance(text, str) or not text.strip()
                or story.get("review_status") != "APPROVED"
                or story.get("reusable") is not True
                or not isinstance(allowed_uses, list)
                or "APPLICATION_ANSWER" not in allowed_uses
                or not isinstance(fact_ids, list) or not fact_ids
                or any(not isinstance(fact_id, str) or not fact_id.strip()
                       for fact_id in fact_ids)
                or not isinstance(fingerprints, list) or not fingerprints):
            continue

        fact_provenance = []
        for fingerprint in fingerprints:
            if not isinstance(fingerprint, dict):
                fact_provenance = []
                break
            fact_id = fingerprint.get("fact_id")
            fact_value = fingerprint.get("fingerprint")
            if (not isinstance(fact_id, str) or not fact_id.strip()
                    or not isinstance(fact_value, str) or not fact_value.strip()):
                fact_provenance = []
                break
            fact_provenance.append({"fact_id": fact_id, "fingerprint": fact_value})
        if (not fact_provenance
                or len(fact_provenance) != len(fact_ids)
                or {item["fact_id"] for item in fact_provenance} != set(fact_ids)):
            continue

        references.append({
            "story_id": story_id,
            "revision": revision,
            "current_revision": revision,
            "is_current": True,
            "title": title,
            "text": text,
            "allowed_use": "APPLICATION_ANSWER",
            "fact_provenance": fact_provenance,
            "review_status": "APPROVED",
        })
    return references


def _story_revision_binding(references):
    """Return the exact current story revisions represented by citations."""
    return tuple(sorted(
        (reference["story_id"], reference["revision"])
        for reference in references
    ))


def create_application_answer_matching_router(store) -> APIRouter:
    """Build an advisory matching endpoint; selection and export remain separate."""
    router = APIRouter()

    @router.post("/profiles/{profile_id}/applications/{application_id}/answers/match")
    def match_application_answers(profile_id: str, application_id: str,
                                  payload: object = Body(...)):
        if (not isinstance(payload, dict)
                or set(payload) not in ({"question"}, {"question", "expected_fingerprint"})
                or not isinstance(payload.get("question"), str)
                or not payload["question"].strip() or len(payload["question"]) > 2000):
            raise HTTPException(422, {"code": "ANSWER_MATCH_INVALID_REQUEST"})
        if ("expected_fingerprint" in payload
                and (not isinstance(payload["expected_fingerprint"], str)
                     or re.fullmatch(r"[0-9a-f]{64}", payload["expected_fingerprint"]) is None)):
            raise HTTPException(422, {"code": "ANSWER_MATCH_INVALID_REVIEW_FINGERPRINT"})
        question = payload["question"].strip()
        if (_SENSITIVE_QUESTION.search(question)
                or is_review_sensitive_question(question)):
            raise HTTPException(422, {
                "code": "ANSWER_MATCH_SENSITIVE_QUESTION",
                "review_gate": {
                    "review_required": True,
                    "reason": "SENSITIVE_QUESTION",
                },
            })

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

        job = _application_job(store, profile_id, application)
        job_fingerprint = target_fingerprint(job)

        policy = answer_service.get_answer_confidence_policy(store, profile_id)
        answer_review = assess_answer_review(
            question,
            profile.get("answers", []),
            policy,
            expected_fingerprint=payload.get("expected_fingerprint"),
        )
        matching_answer_ids = {
            match["answer_id"] for match in answer_review["matches"]
        }
        candidates = _candidate_matches(question, profile.get("answers", []), policy)
        if answer_review["review_required"]:
            candidates = []
        else:
            candidates = [candidate for candidate in candidates
                          if candidate["answer_id"] in matching_answer_ids]
        story_references = _story_reference_rows(store, profile_id)

        try:
            current_profile = store.profile(profile_id)
            current_application = store.application(application_id)
            current_job = _application_job(store, profile_id, current_application)
        except NoResultFound:
            raise HTTPException(
                409, {"code": "ANSWER_MATCH_CONTEXT_STALE"},
            ) from None
        if (current_application.get("profile_id") != profile_id
                or current_application.get("job_id") != job["id"]
                or current_profile.get("version") != profile.get("version")
                or target_fingerprint(current_job) != job_fingerprint):
            raise HTTPException(
                409, {"code": "ANSWER_MATCH_CONTEXT_STALE"},
            )

        current_policy = answer_service.get_answer_confidence_policy(store, profile_id)
        current_answer_review = assess_answer_review(
            question,
            current_profile.get("answers", []),
            current_policy,
            expected_fingerprint=answer_review["fingerprint"],
        )
        if current_answer_review["reason"] == "CHANGED_ANSWER":
            raise HTTPException(
                409, {"code": "ANSWER_MATCH_CONTEXT_STALE"},
            )

        # Story revisions can change independently of the application/profile/job
        # snapshot. Re-read immediately before returning so a superseded or newly
        # pending revision is never projected as current citation metadata.
        current_story_references = _story_reference_rows(
            store, profile_id,
        )
        if (_story_revision_binding(current_story_references)
                != _story_revision_binding(story_references)):
            raise HTTPException(
                409, {"code": "ANSWER_MATCH_CONTEXT_STALE"},
            )

        return {
            "profile_id": profile_id,
            "application_id": application_id,
            "question": question,
            "candidates": candidates,
            "review_gate": {
                "review_required": answer_review["review_required"],
                "reason": answer_review["reason"],
                "fingerprint": answer_review["fingerprint"],
            },
            "story_references": story_references,
        }

    return router
