"""Public answer suggestions reject a stale review after a durable profile edit."""
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration import test_application_answer_matching_api as answer_fixture


def test_matching_profile_edit_and_reopen_rejects_previous_review():
    fixture = answer_fixture.ApplicationAnswerMatchingApiTests()
    fixture.setUp()
    try:
        url = fixture._url()
        question = "Which IT service management tools have you used?"
        before = fixture.client.post(url, json={"question": question})
        assert before.status_code == 200, before.text
        pending = before.json()["review_gate"]
        assert pending["review_required"] is False
        assert [row["answer_id"] for row in before.json()["candidates"]] == ["semantic-itsm"]

        # Reopen the actual persisted workspace before resuming the same review.
        uri = str(fixture.store.engine.url)
        fixture.client.close()
        fixture.store.engine.dispose()
        fixture.store = Store(uri)
        fixture.client = TestClient(create_app(
            fixture.store, "http://127.0.0.1:1", fixture.root, operation_mode="FIXTURE"))
        same = fixture.client.post(url, json={"question": question,
                                             "expected_fingerprint": pending["fingerprint"]})
        assert same.status_code == 200, same.text
        assert same.json()["review_gate"] == pending

        profile = fixture.client.get("/profiles/fixture").json()
        payload = {key: profile[key] for key in ("id", "name", "email", "contact", "preferences")}
        payload["facts"] = [{key: row[key] for key in (
            "id", "kind", "text", "source", "verified", "user_approved", "confidence")}
            for row in profile["facts"]]
        payload["answers"] = [{key: row[key] for key in (
            "id", "question", "text", "source", "verified", "user_approved", "confidence") if key in row}
            for row in profile["answers"]]
        answer = next(row for row in payload["answers"] if row["id"] == "semantic-itsm")
        answer["text"] = "A changed synthetic answer requiring fresh review."
        payload["expected_version"] = profile["version"]
        saved = fixture.client.put("/profiles", json=payload)
        assert saved.status_code == 200, saved.text

        readback = fixture.client.get("/profiles/fixture").json()
        edited = next(row for row in readback["answers"] if row["id"] == "semantic-itsm")
        assert edited["text"] == answer["text"]
        assert edited["user_approved"] is False
        stale = fixture.client.post(url, json={"question": question,
                                              "expected_fingerprint": pending["fingerprint"]})
        assert stale.status_code == 200, stale.text
        assert stale.json()["review_gate"]["reason"] == "CHANGED_ANSWER"
        assert stale.json()["candidates"] == []
        fresh = fixture.client.post(url, json={"question": question})
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["review_gate"]["reason"] == "UNKNOWN_ANSWER"
        assert fresh.json()["candidates"] == []
    finally:
        fixture.client.close()
        fixture.tearDown()
