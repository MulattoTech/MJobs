"""Public fixture intent admission connected to physical attempt ownership."""
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict, start_attempt
from tests.integration import test_application_run_requests as fixtures
from workers.operations import claim, request_cancel


@pytest.fixture
def prepared():
    case = fixtures.ApplicationRunRequestTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def reserve(case, request_id):
    application = case.store.application(case.app_id)
    with case.store.engine.connect() as connection:
        route = dict(connection.execute(select(m.routes).where(
            m.routes.c.id == application["route_id"])).mappings().one())
        documents = list(connection.execute(select(m.documents).where(
            m.documents.c.application_id == case.app_id,
            m.documents.c.version == application["document_version"])).mappings())
    return start_attempt(case.store, application, route, documents, request_id=request_id)


def test_concurrent_public_duplicate_is_one_durable_intent_after_restart(prepared):
    case = prepared
    gate = threading.Barrier(2)
    endpoint = f"/profiles/fixture/applications/{case.app_id}/mock-run"

    def post(_):
        store = Store(case.store.url)
        try:
            with TestClient(create_app(store, "http://127.0.0.1:8090", case.root,
                                       operation_mode="FIXTURE")) as client:
                gate.wait(timeout=10)
                response = client.post(endpoint, json={"submit": False, "request_id": "lost-reply"})
                assert response.status_code == 202, response.text
                return response.json()
        finally:
            store.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, range(2)))
    assert len({row["operation_id"] for row in results}) == 1
    assert sum(not row["replayed"] for row in results) == 1
    assert case.store.count("worker_operations") == 1
    assert case.store.count("application_attempts") == 0

    case.store.engine.dispose()
    reopened = Store(case.store.url)
    try:
        with TestClient(create_app(reopened, "http://127.0.0.1:8090", case.root,
                                   operation_mode="FIXTURE")) as client:
            result = client.get(endpoint + "s/lost-reply")
            assert result.status_code == 200
            assert result.json()["operation_id"] == results[0]["operation_id"]
            assert result.json()["state"] == "QUEUED"
            assert result.json()["external_submission_enabled"] is False
            assert client.get(endpoint.replace("/fixture/", "/another-profile/")
                              + "s/lost-reply").status_code == 404
    finally:
        reopened.engine.dispose()


def test_queued_public_intent_fences_standalone_attempt_then_owner_can_reserve(prepared):
    case = prepared
    endpoint = f"/profiles/fixture/applications/{case.app_id}/mock-run"
    with TestClient(create_app(case.store, "http://127.0.0.1:8090", case.root,
                               operation_mode="FIXTURE")) as client:
        queued = client.post(endpoint, json={"submit": False, "request_id": "reviewed"})
        assert queued.status_code == 202, queued.text
        operation_id = queued.json()["operation_id"]
        with pytest.raises(AttemptConflict, match="APPLICATION_RUN_IN_PROGRESS"):
            reserve(case, "unrelated-browser-call")
        assert case.store.count("application_attempts") == 0
        assert claim(case.store, operation_id) is not None
        attempt = reserve(case, operation_id)
        assert attempt["replayed"] is False
        history = client.get(f"/profiles/fixture/applications/{case.app_id}/attempt-history")
        assert history.status_code == 200, history.text
        assert history.json()["attempts"][0]["attempt_id"] == attempt["id"]
        assert history.json()["attempts"][0]["state"] == "RUNNING"


def test_cancelled_public_intent_cannot_open_a_new_physical_attempt(prepared):
    case = prepared
    operation_id = case.queue("cancelled-before-browser")["operation_id"]
    assert request_cancel(case.store, operation_id)["state"] == "CANCELLED"
    with pytest.raises(AttemptConflict):
        reserve(case, operation_id)
    assert case.store.count("application_attempts") == 0
    assert case.operation(operation_id)["state"] == "CANCELLED"


def test_concurrent_distinct_public_intents_have_one_owner(prepared):
    case = prepared
    gate = threading.Barrier(2)
    endpoint = f"/profiles/fixture/applications/{case.app_id}/mock-run"

    def post(request_id):
        store = Store(case.store.url)
        try:
            with TestClient(create_app(store, "http://127.0.0.1:8090", case.root,
                                       operation_mode="FIXTURE")) as client:
                gate.wait(timeout=10)
                response = client.post(endpoint, json={"submit": False, "request_id": request_id})
                return response.status_code, response.json()
        finally:
            store.engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, ("intent-one", "intent-two")))
    assert sorted(status for status, _ in results) == [202, 409]
    rejected = next(body for status, body in results if status == 409)
    assert rejected["detail"]["code"] == "APPLICATION_RUN_IN_PROGRESS"
    assert case.store.count("worker_operations") == 1
    assert case.store.count("application_attempts") == 0
