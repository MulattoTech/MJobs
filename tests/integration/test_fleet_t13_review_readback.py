"""Synthetic durable answer snapshots survive reopen and reject stale reviews."""
from copy import deepcopy

import pytest
from sqlalchemy import update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.answers.review import assess_answer_review


QUESTION = "Can you work weekends?"


def test_answer_review_reopens_and_detects_durable_edit(tmp_path):
    uri = f"sqlite:///{(tmp_path / 'review.db').as_posix()}"
    store = Store(uri)
    store.migrate()
    store.save_profile({
        "id": "synthetic-t13", "name": "Synthetic Reviewer", "email": "t13@example.test", "facts": [],
        "answers": [{"id": "weekends", "question": QUESTION, "text": "Yes",
                     "source": "Synthetic user entry", "verified": True,
                     "user_approved": True, "confidence": 100}],
    })
    original = store.profile("synthetic-t13")
    pending = assess_answer_review(QUESTION, original["answers"])
    assert pending["review_required"] is False
    store.engine.dispose()

    reopened = Store(uri)
    try:
        current = reopened.profile("synthetic-t13")
        assert assess_answer_review(QUESTION, current["answers"],
                                    expected_fingerprint=pending["fingerprint"]) == pending

        # A write interrupted before commit must leave the old snapshot intact.
        interrupted = deepcopy(current["answers"])
        interrupted[0]["text"] = "No"
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            with reopened.engine.begin() as connection:
                connection.execute(update(m.profile_versions).where(
                    m.profile_versions.c.profile_id == "synthetic-t13",
                    m.profile_versions.c.version == current["version"]).values(answers=interrupted))
                raise RuntimeError("synthetic interruption")
        intact = reopened.profile("synthetic-t13")
        assert intact["answers"][0]["text"] == "Yes"
        assert assess_answer_review(QUESTION, intact["answers"],
                                    expected_fingerprint=pending["fingerprint"]) == pending

        # A committed user edit clears approval in the real profile store.
        edited = {**current, "answers": interrupted}
        reopened.save_profile(edited)
        changed = reopened.profile("synthetic-t13")
        assert changed["answers"][0]["user_approved"] is False
        resumed = assess_answer_review(QUESTION, changed["answers"],
                                       expected_fingerprint=pending["fingerprint"])
        assert resumed["reason"] == "CHANGED_ANSWER"
        assert resumed["matches"] == []
        assert assess_answer_review(QUESTION, changed["answers"])["reason"] == "UNKNOWN_ANSWER"
    finally:
        reopened.engine.dispose()
