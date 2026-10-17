"""Real synthetic worker admission must retain answer gates before Chromium."""
from copy import deepcopy
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import mock_adapter
from services.applications.preflight import latest_preflight
from services.discovery.pipeline import MockClient, discover
from services.documents.answers import save_answer_confidence_policy
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


def metadata(label="Country"):
    return {"provider": "mock-ats", "tenant": "example", "requisition": "B", "revision": 1,
            "questions": [{"id": "country", "label": label, "required": True,
                           "type": "text", "requirements": {"answer_ids": ["country"]}}]}


@pytest.fixture
def worker_workspace(tmp_path):
    with fixture_server() as origin:
        store = Store(f"sqlite:///{(tmp_path / 'worker.db').as_posix()}")
        store.migrate()
        profile = deepcopy(PROFILE)
        labels = {"country": "Country", "remote": "Remote role", "fixture_question": "Approved fixture answer"}
        for row in profile["answers"]:
            row["question"] = labels[row["id"]]
        yield store, origin, tmp_path, profile
        store.engine.dispose()


def prepared(workspace, profile):
    store, origin, root, _ = workspace
    store.save_profile(profile)
    discover(store, origin, "fixture")
    job = next(row for row in store.jobs() if row["requisition"] == "B")
    return prepare(store, job["id"], "fixture", root)


@pytest.mark.parametrize("scenario", ["low_confidence", "conflict", "wrong_question", "sensitive"])
def test_worker_gate_survives_slot_normalization(worker_workspace, scenario):
    store, origin, root, profile = worker_workspace
    if scenario == "low_confidence":
        profile["answers"][0]["confidence"] = 90
    elif scenario == "conflict":
        profile["answers"].append({**profile["answers"][0], "id": "other-country", "text": "Canada"})
    elif scenario == "wrong_question":
        profile["answers"][0]["question"] = "Which work schedule do you prefer?"
    application_id = prepared(worker_workspace, profile)
    save_answer_confidence_policy(store, "fixture", {
        "minimum_saved_answer_confidence": 95, "minimum_question_match_confidence": 0.75,
    })
    questions = metadata("What is your social security number?" if scenario == "sensitive" else "Country")
    with patch.object(mock_adapter, "_read_questionnaire", return_value=questions), \
         patch.object(mock_adapter, "sync_playwright", side_effect=AssertionError("browser must not start")) as browser:
        result = mock_adapter.apply_mock(store, application_id, origin, root, submit=False)
    assert result["state"] == "REVIEW_REQUIRED"
    assert store.count("application_attempts") == 0
    browser.assert_not_called()
    stored = latest_preflight(store, application_id)
    assert stored["result"] != "PASS"
    if scenario == "conflict":
        assert stored["answer_review"][0]["reason"] == "CONFLICTING_ANSWERS"
    elif scenario == "wrong_question":
        assert stored["answer_review"][0]["reason"] == "UNKNOWN_ANSWER"
    elif scenario == "sensitive":
        assert stored["answer_review"][0]["reason"] == "SENSITIVE_QUESTION"
    assert "Canada" not in str(stored)


@pytest.mark.parametrize("failure", ["unavailable", "changed_after_metadata", "changed_before_browser",
                                    "answer_changed_before_browser"])
def test_policy_failure_or_change_blocks_browser_admission(worker_workspace, failure):
    store, origin, root, profile = worker_workspace
    application_id = prepared(worker_workspace, profile)

    def change_policy():
        save_answer_confidence_policy(store, "fixture", {
            "minimum_saved_answer_confidence": 100, "minimum_question_match_confidence": 1.0,
        })

    reads = 0

    def read_metadata(*args):
        nonlocal reads
        reads += 1
        if failure == "changed_after_metadata" and reads == 2:
            change_policy()
        return metadata()

    from services.applications import attempt_history
    original_start = attempt_history.start_attempt

    def start_with_changed_policy(*args, **kwargs):
        result = original_start(*args, **kwargs)
        if failure == "answer_changed_before_browser":
            edited = store.profile("fixture")
            edited["answers"][0]["text"] = "Changed synthetic country answer"
            store.save_profile(edited)
        else:
            change_policy()
        return result

    with patch.object(mock_adapter, "_read_questionnaire", side_effect=read_metadata), \
         patch.object(mock_adapter, "sync_playwright", side_effect=AssertionError("browser must not start")) as browser:
        if failure == "unavailable":
            with patch.object(mock_adapter, "get_answer_confidence_policy", side_effect=RuntimeError("SYNTHETIC_PRIVATE_ERROR")):
                result = mock_adapter.apply_mock(store, application_id, origin, root, submit=False)
        elif failure in {"changed_before_browser", "answer_changed_before_browser"}:
            with patch.object(attempt_history, "start_attempt", side_effect=start_with_changed_policy):
                result = mock_adapter.apply_mock(store, application_id, origin, root, submit=False)
        else:
            result = mock_adapter.apply_mock(store, application_id, origin, root, submit=False)
    assert result["state"] == "REVIEW_REQUIRED"
    browser.assert_not_called()
    stored = latest_preflight(store, application_id)
    expected = {"unavailable": "ANSWER_POLICY_UNAVAILABLE",
                "answer_changed_before_browser": "CHANGED_ANSWER"}.get(failure, "STALE_ANSWER_POLICY")
    assert stored["reason"] == expected
    assert stored["result"] == "UNKNOWN"
    assert "SYNTHETIC_PRIVATE_ERROR" not in str(stored)
    if "before_browser" not in failure:
        assert store.count("application_attempts") == 0


def test_public_profile_api_numeric_confidence_reaches_passed_worker_preflight(worker_workspace):
    store, origin, root, profile = worker_workspace
    with TestClient(create_app(store, origin, root, operation_mode="FIXTURE")) as client:
        saved = client.put("/profiles", json=profile)
        assert saved.status_code == 200, saved.text
        persisted = client.get("/profiles/fixture").json()
        assert type(persisted["answers"][0]["confidence"]) is float
        assert persisted["answers"][0]["confidence"] == 100.0
        assert persisted["answers"][0]["user_approved"] is True

        discover(store, origin, "fixture")
        job = next(row for row in store.jobs() if row["requisition"] == "B")
        application_id = prepare(store, job["id"], "fixture", root)
        application = store.application(application_id)
        with store.engine.connect() as connection:
            route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == application["route_id"])).mappings().one())
        source = store.profile("fixture")
        mock_client = MockClient(origin)
        try:
            with patch.object(mock_adapter, "_read_questionnaire", return_value=metadata()):
                result = mock_adapter._preflight(
                    store, application, route, source, {"resume", "cover-letter"}, mock_client)
            assert result["result"] == "PASS"
            readback = latest_preflight(store, application_id)
            assert readback["answer_review"][0]["review_required"] is False
            assert readback["answer_review"][0]["reason"] is None
        finally:
            mock_client.close()
