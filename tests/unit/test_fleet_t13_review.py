"""Answer reuse must fail closed before consumers expose or map a suggestion."""
from copy import deepcopy

import pytest

from services.answers.review import assess_answer_review


QUESTION = "Can you work weekends?"


def saved(answer_id="weekends", text="Yes"):
    return {"id": answer_id, "question": QUESTION, "text": text,
            "source": "Synthetic user entry", "confidence": 100,
            "verified": True, "user_approved": True}


def test_approved_paraphrase_is_available_but_unknown_is_review_required():
    result = assess_answer_review("Are you available to work weekends?", [saved()])
    assert result["review_required"] is False
    assert result["matches"] == [{"answer_id": "weekends", "method": "CURATED_SEMANTIC", "score": 0.95}]
    assert assess_answer_review("What is your favorite editor?", [saved()])["reason"] == "UNKNOWN_ANSWER"


def test_conflicting_approved_answers_are_not_selectable():
    result = assess_answer_review(QUESTION, [saved(), saved("other", "No")])
    assert result["reason"] == "CONFLICTING_ANSWERS"
    assert result["review_required"] is True
    assert result["matches"] == []
    assert "Yes" not in str(result) and "No" not in str(result)


@pytest.mark.parametrize("question", ["What is your military service?", "What is your national origin?",
                                      "Are you legally authorized to work?", "Do you have a medical condition?"])
def test_sensitive_and_legal_answers_cannot_be_reused(question):
    answer = {**saved(), "question": question}
    result = assess_answer_review(question, [answer])
    assert result["reason"] == "SENSITIVE_QUESTION"
    assert result["matches"] == []


@pytest.mark.parametrize("change", [{"text": "No"}, {"source": "Changed source"},
                                    {"user_approved": False}, {"requires_review": True}])
def test_changed_review_snapshot_cannot_resume(change):
    answer = saved()
    before = assess_answer_review(QUESTION, [answer])
    result = assess_answer_review(QUESTION, [{**answer, **change}], expected_fingerprint=before["fingerprint"])
    assert result["reason"] == "CHANGED_ANSWER"
    assert result["matches"] == []
    assert result["review_required"] is True


def test_snapshot_is_stable_across_readback_and_answer_order():
    rows = [saved(), saved("duplicate", "Yes")]
    original = deepcopy(rows)
    before = assess_answer_review(QUESTION, rows)
    after = assess_answer_review(QUESTION, list(reversed(rows)), expected_fingerprint=before["fingerprint"])
    assert before == after
    assert rows == original


@pytest.mark.parametrize("change", [{"verified": False}, {"confidence": 79}, {"source": ""},
                                    {"requires_review": True}, {"allowed_for_auto_use": False}])
def test_unapproved_or_review_only_answers_do_not_supply_matches(change):
    result = assess_answer_review(QUESTION, [{**saved(), **change}])
    assert result["review_required"] is True
    assert result["matches"] == []


def test_policy_change_invalidates_prior_snapshot():
    before = assess_answer_review(QUESTION, [saved()])
    result = assess_answer_review(QUESTION, [saved()], {
        "minimum_saved_answer_confidence": 100, "minimum_question_match_confidence": 1.0,
    }, expected_fingerprint=before["fingerprint"])
    assert result["reason"] == "CHANGED_ANSWER"
