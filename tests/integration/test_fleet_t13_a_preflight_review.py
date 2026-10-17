"""Connected preflight answer-review gate acceptance using synthetic profiles."""
from pathlib import Path
import tempfile

from packages.persistence.store import Store
from services.applications.preflight import (
    build_preflight_result,
    latest_preflight,
    persist_preflight,
)


ROUTE = {"provider": "mock-ats", "tenant": "example", "requisition": "T13"}
QUESTION = "Are you available to work weekends?"
ANSWER = "Yes, I can work weekends."


def saved(answer_id="weekends", text=ANSWER, question="Can you work weekends?"):
    return {
        "id": answer_id,
        "question": question,
        "text": text,
        "source": "Synthetic user entry",
        "confidence": 100,
        "verified": True,
        "user_approved": True,
    }


def questionnaire(*, label=QUESTION, answer_ids=("weekends",)):
    return {
        "provider": "mock-ats",
        "tenant": "example",
        "requisition": "T13",
        "revision": 1,
        "questions": [{
            "id": "weekends",
            "label": label,
            "required": True,
            "requirements": {"answer_ids": list(answer_ids)},
        }],
    }


def evaluate(answers, metadata=None, expected=None):
    profile = {"version": 1, "answers": answers}
    return build_preflight_result(
        profile, ROUTE, metadata or questionnaire(), {"resume"},
        expected_answer_review_fingerprints=expected,
    )


def test_question_bearing_approved_answer_is_gated_and_legacy_id_only_still_works():
    result = evaluate([saved()])
    assert result["result"] == "PASS"
    assert result["answer_review"] == [{
        "question_id": "weekends",
        "fingerprint": result["answer_review"][0]["fingerprint"],
        "reason": None,
        "review_required": False,
    }]
    assert ANSWER not in str(result)

    legacy_profile = {"version": 1, "answers": [{
        "id": "weekends", "text": ANSWER, "source": "Synthetic provider slot",
        "confidence": 100, "verified": True, "user_approved": True,
    }]}
    legacy = build_preflight_result(legacy_profile, ROUTE, questionnaire(), {"resume"})
    assert legacy["result"] == "PASS"
    assert "answer_review" not in legacy
    legacy_with_expected_gate = build_preflight_result(
        legacy_profile, ROUTE, questionnaire(), {"resume"},
        expected_answer_review_fingerprints={},
    )
    assert legacy_with_expected_gate["result"] == "UNKNOWN"
    assert legacy_with_expected_gate["answer_review"][0]["reason"] == "CHANGED_ANSWER"


def test_raw_review_source_survives_normalization_and_binds_conflicts_changes_and_policy():
    raw_answers = [saved()]
    normalized = saved()
    normalized.pop("question")
    profile = {"version": 1, "answers": [normalized]}
    baseline = build_preflight_result(
        profile, ROUTE, questionnaire(), {"resume"},
        review_profile_answers=raw_answers,
    )
    assert baseline["result"] == "PASS"

    conflict = build_preflight_result(
        profile, ROUTE, questionnaire(), {"resume"},
        review_profile_answers=[*raw_answers, saved("other", "No, I cannot work weekends.")],
    )
    assert conflict["result"] == "UNKNOWN"
    assert conflict["answer_review"][0]["reason"] == "CONFLICTING_ANSWERS"

    changed_raw = [saved(text="No, I cannot work weekends.")]
    changed_normalized = [{**changed_raw[0]}]
    changed_normalized[0].pop("question")
    changed = build_preflight_result(
        {"version": 1, "answers": changed_normalized}, ROUTE, questionnaire(), {"resume"},
        expected_answer_review_fingerprints={
            "weekends": baseline["answer_review"][0]["fingerprint"],
        },
        review_profile_answers=changed_raw,
    )
    assert changed["result"] == "UNKNOWN"
    assert changed["answer_review"][0]["reason"] == "CHANGED_ANSWER"

    strict_policy = build_preflight_result(
        profile, ROUTE, questionnaire(), {"resume"},
        answer_confidence_policy={
            "minimum_saved_answer_confidence": 100,
            "minimum_question_match_confidence": 1.0,
        },
        review_profile_answers=raw_answers,
    )
    assert strict_policy["result"] == "UNKNOWN"
    assert strict_policy["answer_review"][0]["reason"] == "UNKNOWN_ANSWER"

    low_confidence_raw = [saved()]
    low_confidence_raw[0]["confidence"] = 90
    low_confidence_normalized = [{key: value for key, value in low_confidence_raw[0].items()
                                  if key != "question"}]
    saved_threshold_policy = {
        "minimum_saved_answer_confidence": 95,
        "minimum_question_match_confidence": 0.75,
    }
    low_question_answer = build_preflight_result(
        {"version": 1, "answers": low_confidence_normalized},
        ROUTE, questionnaire(), {"resume"},
        answer_confidence_policy=saved_threshold_policy,
        review_profile_answers=low_confidence_raw,
    )
    assert low_question_answer["result"] == "UNKNOWN"
    assert low_question_answer["answer_review"][0]["reason"] == "UNKNOWN_ANSWER"

    low_legacy_answer = build_preflight_result(
        {"version": 1, "answers": low_confidence_normalized},
        ROUTE, questionnaire(), {"resume"},
        answer_confidence_policy=saved_threshold_policy,
        review_profile_answers=low_confidence_normalized,
    )
    assert low_legacy_answer["result"] == "FAIL"
    assert low_legacy_answer["missing"][0]["answer_ids"] == ["weekends"]

    malformed = build_preflight_result(
        profile, ROUTE, questionnaire(), {"resume"},
        review_profile_answers=[*raw_answers, "malformed row"],
    )
    assert malformed["result"] == "UNKNOWN"
    assert malformed["answer_review"][0]["reason"] == "UNKNOWN_ANSWER"

    raw_legacy = [{key: value for key, value in raw_answers[0].items() if key != "question"}]
    legacy = build_preflight_result(
        profile, ROUTE, questionnaire(), {"resume"}, review_profile_answers=raw_legacy,
    )
    assert legacy["result"] == "PASS"
    assert "answer_review" not in legacy


def test_conflict_sensitive_and_changed_answers_require_review():
    # The competing approved answer is not in provider mapping IDs; the shared
    # review must still inspect the full profile answer snapshot.
    conflict = evaluate([saved(), saved("other", "No, I cannot work weekends.")])
    assert conflict["result"] == "UNKNOWN"
    assert conflict["reason"] == "ANSWER_REVIEW_CONFLICTING_ANSWERS"
    assert conflict["answer_review"][0]["reason"] == "CONFLICTING_ANSWERS"
    assert conflict["answer_review"][0]["review_required"] is True
    assert "cannot work" not in str(conflict)

    sensitive = evaluate([saved(question="What is your national origin?")],
                         questionnaire(label="What is your national origin?"))
    assert sensitive["result"] == "UNKNOWN"
    assert sensitive["reason"] == "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION"
    assert sensitive["answer_review"][0]["reason"] == "SENSITIVE_QUESTION"
    assert sensitive["answer_review"][0]["review_required"] is True

    original = evaluate([saved()])
    changed = evaluate([saved(text="No, I cannot work weekends.")], expected={
        "weekends": original["answer_review"][0]["fingerprint"],
    })
    assert changed["result"] == "UNKNOWN"
    assert changed["reason"] == "ANSWER_REVIEW_CHANGED_ANSWER"
    assert changed["answer_review"][0]["reason"] == "CHANGED_ANSWER"
    assert changed["answer_review"][0]["review_required"] is True

    policy_gated = build_preflight_result(
        {"version": 1, "answers": [saved()]}, ROUTE, questionnaire(), {"resume"},
        answer_confidence_policy={
            "minimum_saved_answer_confidence": 100,
            "minimum_question_match_confidence": 1.0,
        },
    )
    assert policy_gated["result"] == "UNKNOWN"
    assert policy_gated["reason"] == "ANSWER_REVIEW_UNKNOWN_ANSWER"

    unmapped_fingerprint = evaluate([saved()], expected={})
    malformed_fingerprint = evaluate([saved()], expected={"weekends": None})
    assert unmapped_fingerprint["result"] == "UNKNOWN"
    assert unmapped_fingerprint["answer_review"][0]["reason"] == "CHANGED_ANSWER"
    assert malformed_fingerprint["result"] == "UNKNOWN"
    assert malformed_fingerprint["answer_review"][0]["reason"] == "CHANGED_ANSWER"


def test_optional_review_gate_is_recorded_without_changing_missing_answer_semantics():
    optional = questionnaire(answer_ids=("weekends",))
    optional["questions"][0]["required"] = False
    optional_result = evaluate([saved(question="What is your favorite color?")], optional)
    assert optional_result["result"] == "PASS"
    assert optional_result["missing"] == []
    assert optional_result["answer_review"][0]["review_required"] is True

    unapproved = saved()
    unapproved["user_approved"] = False
    question_bearing = evaluate([unapproved])
    assert question_bearing["result"] == "UNKNOWN"
    assert question_bearing["answer_review"][0]["reason"] == "UNKNOWN_ANSWER"

    legacy_unapproved = {key: value for key, value in unapproved.items() if key != "question"}
    missing = evaluate([legacy_unapproved])
    assert missing["result"] == "FAIL"
    assert missing["missing"][0]["answer_ids"] == ["weekends"]
    assert "answer_review" not in missing


def test_shared_sensitive_classifier_blocks_required_question_without_saved_answer():
    metadata = questionnaire(
        label="Do restrictive covenants limit your employment?", answer_ids=(),
    )
    result = evaluate([], metadata)
    assert result["result"] == "UNKNOWN"
    assert result["reason"] == "ANSWER_REVIEW_SENSITIVE_QUESTION"
    assert result["answer_review"][0]["reason"] == "SENSITIVE_QUESTION"
    assert result["answer_review"][0]["review_required"] is True


def test_unknown_gate_metadata_survives_sqlite_readback_without_answer_values():
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "review.db"
        store = Store(f"sqlite:///{database.as_posix()}")
        store.migrate()
        sensitive_value = "SYNTHETIC_PRIVATE_ANSWER_DO_NOT_PERSIST"
        result = evaluate([saved(question="What is your social security number?",
                                 text=sensitive_value)], questionnaire(
                                     label="What is your social security number?"))
        result["profile_version"] = 1
        result["document_version"] = 1
        persist_preflight(store, "synthetic-application", result)
        store.engine.dispose()

        reopened = Store(f"sqlite:///{database.as_posix()}")
        stored = latest_preflight(reopened, "synthetic-application")
        assert stored["result"] == "UNKNOWN"
        assert stored["reason"] == "UNEXPECTED_SENSITIVE_REQUIRED_QUESTION"
        assert stored["answer_review"] == [{
            "question_id": "weekends",
            "fingerprint": result["answer_review"][0]["fingerprint"],
            "reason": "SENSITIVE_QUESTION",
            "review_required": True,
        }]
        assert sensitive_value not in str(stored)
        reopened.engine.dispose()
