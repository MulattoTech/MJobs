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
                                      "Are you legally authorized to work?", "Do you have a medical condition?",
                                      "What is your social security number?", "Have you been arrested?",
                                      "Are you 18 years or older?", "What is your expected salary?"])
def test_sensitive_and_legal_answers_cannot_be_reused(question):
    answer = {**saved(), "question": question}
    result = assess_answer_review(question, [answer])
    assert result["reason"] == "SENSITIVE_QUESTION"
    assert result["matches"] == []


@pytest.mark.parametrize("change", [{"text": "No"}, {"source": "Changed source"},
                                    {"question": "Are you available to work weekends?"},
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


def test_saved_sensitive_wording_cannot_enter_ordinary_fuzzy_matches():
    question = "Please provide your current social number for this position"
    answer = {**saved(), "question": "Please provide your current social security number for this position"}
    result = assess_answer_review(question, [answer])
    assert result["matches"] == []
    assert result["review_required"] is True


@pytest.mark.parametrize("question, rows", [(None, []), ("", []), (QUESTION, None),
                                          (QUESTION, [None]), ("x" * 2001, [saved()])])
def test_malformed_snapshots_never_supply_matches(question, rows):
    result = assess_answer_review(question, rows)
    assert result["reason"] == "UNKNOWN_ANSWER"
    assert result["matches"] == []


@pytest.mark.parametrize("fingerprint", ["", "invalid", 123, "f" * 64])
def test_invalid_or_unrelated_resume_binding_requires_review(fingerprint):
    result = assess_answer_review(QUESTION, [saved()], expected_fingerprint=fingerprint)
    assert result["reason"] == "CHANGED_ANSWER"
    assert result["matches"] == []


@pytest.mark.parametrize("confidence", [100.0, 99.5, 80.0])
def test_finite_public_api_confidence_is_reusable(confidence):
    result = assess_answer_review(QUESTION, [{**saved(), "confidence": confidence}])
    assert result["review_required"] is False
    assert [match["answer_id"] for match in result["matches"]] == ["weekends"]


@pytest.mark.parametrize("confidence", [True, False, float("nan"), float("inf"), -float("inf")])
def test_boolean_and_nonfinite_confidence_cannot_supply_answers(confidence):
    result = assess_answer_review(QUESTION, [{**saved(), "confidence": confidence}])
    assert result["review_required"] is True
    assert result["matches"] == []


def test_changed_finite_float_confidence_invalidates_review_binding():
    pending = assess_answer_review(QUESTION, [{**saved(), "confidence": 99.5}])
    resumed = assess_answer_review(QUESTION, [{**saved(), "confidence": 98.5}],
                                   expected_fingerprint=pending["fingerprint"])
    assert resumed["reason"] == "CHANGED_ANSWER"
    assert resumed["matches"] == []
