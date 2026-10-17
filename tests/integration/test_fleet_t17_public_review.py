"""Public review interruption contracts, using synthetic bytes at the fetch boundary.

This proves API -> durable operation -> worker -> reopened API, not Redis dispatch,
rendered DOM visibility, authenticated provider access, or external submission.
"""
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


ROUTES = [
    ("ashby", "https://jobs.ashbyhq.com/fixture/engineering-123", "engineering-123"),
    ("greenhouse", "https://boards.greenhouse.io/fixture/jobs/12345", "12345"),
    ("lever", "https://jobs.lever.co/fixture/12345678-abcd-1234-abcd-1234567890ab",
     "12345678-abcd-1234-abcd-1234567890ab"),
    ("workday", "https://fixture.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_R123", "R123"),
]
PRIVATE = "FLEET_T17_PRIVATE_VALUE"
HTML = ("<form><label for='email'>Email</label>"
        f"<input id='email' name='email' type='email' value='{PRIVATE}'>"
        "<input id='password' name='password' type='password'>"
        "</form>").encode()


@pytest.fixture(params=ROUTES, ids=[row[0] for row in ROUTES])
def review(tmp_path, request):
    provider, url, requisition = request.param
    database_url = f"sqlite:///{(tmp_path / 'review.db').as_posix()}"
    store = Store(database_url)
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    store.save_profile(dict(PROFILE))
    with fixture_server() as origin:
        discover(store, origin, "fixture")
    job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
    application_id = prepare(store, job["id"], "fixture", tmp_path)
    route_id = store.application(application_id)["route_id"]
    with store.engine.begin() as connection:
        connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
            provider=provider, tenant="fixture", requisition=requisition, url=url))
        connection.execute(update(m.applications).where(
            m.applications.c.id == application_id).values(target="REAL"))
    state = {"store": store, "database_url": database_url, "root": tmp_path,
             "url": url, "application_id": application_id, "route_id": route_id,
             "path": f"/profiles/fixture/applications/{application_id}/form-reviews"}
    yield state
    store.engine.dispose()


@contextmanager
def api(state, *, reopened=False):
    store = Store(state["database_url"]) if reopened else state["store"]
    try:
        with TestClient(create_app(store, "http://127.0.0.1:1", state["root"],
                                   operation_mode="FIXTURE")) as client:
            yield client
    finally:
        if reopened:
            store.engine.dispose()


def execute(state, operation_id, during_fetch=lambda: None):
    fetched = []

    class FixtureClient:
        def __init__(self, website, check=None, total_timeout=None):
            assert website == state["url"]
            self.check = check

        def get(self, url):
            assert url == state["url"]
            self.check()
            fetched.append(url)
            during_fetch()
            return type("Response", (), {"url": url, "content": HTML,
                        "headers": {"content-type": "text/html"}})()

        def allowed(self, url):
            return url == state["url"]

        def close(self):
            pass

    with patch.dict("os.environ", {
        "MJOBS_OPERATION_MODE": "FIXTURE", "MJOBS_DATABASE_URL": state["database_url"],
        "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
        "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
        "MJOBS_OUTPUT_DIR": str(state["root"]),
    }), patch("workers.tasks.PublicCompanyClient", FixtureClient):
        result = execute_operation(operation_id)
    assert fetched == [state["url"]]
    return result


def queue(client, state, request_id):
    response = client.post(state["path"], json={"request_id": request_id})
    assert response.status_code == 202, response.text
    return response.json()["operation_id"]


def receipts(state):
    with state["store"].engine.connect() as connection:
        return list(connection.execute(select(m.audit.c.payload).where(
            m.audit.c.entity_id == state["application_id"],
            m.audit.c.action == "APPLICATION_FORM_REVIEW")).scalars())


def test_cancel_during_fetch_survives_reopen_and_new_request_recovers(review):
    before = review["store"].application(review["application_id"])
    attempts = review["store"].count("application_attempts")
    documents = review["store"].count("documents")
    with api(review) as client:
        operation_id = queue(client, review, "interrupted")

        def cancel():
            response = client.post(f"/operations/{operation_id}/cancel")
            assert response.status_code == 202, response.text

        assert execute(review, operation_id, cancel)["status"] == "CANCELLED"
    assert receipts(review) == []
    with api(review, reopened=True) as client:
        cancelled = client.get(review["path"] + "/interrupted")
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "CANCELLED"
        assert cancelled.json()["analysis"] is None
        # The review endpoint exposes only FORM_REVIEW_* error codes.
        assert cancelled.json()["error_code"] is None
        with review["store"].engine.connect() as connection:
            assert connection.execute(select(m.operations.c.error).where(
                m.operations.c.id == operation_id)).scalar_one() == "CANCEL_REQUESTED"
        recovered_id = queue(client, review, "recovered")
        assert recovered_id != operation_id
        assert execute(review, recovered_id)["status"] == "COMPLETED"
    with api(review, reopened=True) as client:
        exact = client.get(review["path"] + "/recovered")
        latest = client.get(review["path"] + "/latest")
        assert exact.status_code == latest.status_code == 200
        assert exact.json() == latest.json()
        assert exact.json()["state"] == "COMPLETED"
        assert exact.json()["analysis"]["status"] == "REVIEW_REQUIRED"
        assert any(field["review_reason"] == "CREDENTIAL_FIELD"
                   for field in exact.json()["analysis"]["fields"])
        assert client.get(review["path"] + "/interrupted").json() == cancelled.json()
        assert PRIVATE not in exact.text
    assert len(receipts(review)) == 1
    assert PRIVATE not in str(receipts(review))
    assert review["store"].application(review["application_id"]) == before
    assert review["store"].count("application_attempts") == attempts
    assert review["store"].count("documents") == documents


def test_route_replaced_during_fetch_cannot_publish_a_stale_analysis(review):
    with api(review) as client:
        operation_id = queue(client, review, "route-race")

        def change_route():
            with review["store"].engine.begin() as connection:
                connection.execute(update(m.routes).where(
                    m.routes.c.id == review["route_id"]).values(provider="unknown"))

        assert execute(review, operation_id, change_route)["status"] == "DEAD_LETTERED"
    assert receipts(review) == []
    with api(review, reopened=True) as client:
        exact = client.get(review["path"] + "/route-race")
        assert exact.status_code == 200
        assert exact.json()["state"] == "DEAD_LETTERED"
        assert exact.json()["error_code"] == "FORM_REVIEW_INPUT_STALE"
        assert exact.json()["analysis"] is None
        assert PRIVATE not in exact.text
