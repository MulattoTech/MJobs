"""Synthetic public checkpoint recovery without employer traffic or credentials."""
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert


ROUTE = "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply"
URL = "/profiles/profile/applications/application/multipage-state"


def observation(heading, *, url=ROUTE, stale=False):
    return {"url": url, "headings": [heading], "field_labels": [heading],
            "actions": ["Save and Continue"], "stale": stale}


@pytest.fixture
def recovery(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}"
    store = Store(database_url)
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    with store.engine.begin() as connection:
        for table, values in (
            (m.profiles, {"id": "profile", "name": "Synthetic User",
                          "email": "synthetic@example.invalid", "version": "v1"}),
            (m.companies, {"id": "company", "name": "Synthetic", "domain": "example.invalid"}),
            (m.jobs, {"id": "job", "company_id": "company", "requisition": "REQ-1",
                      "title": "Synthetic Role", "description": "Synthetic", "skills": [],
                      "location": "Remote", "remote_type": "REMOTE", "attributes": {}}),
            (m.routes, {"id": "route", "job_id": "job", "company_id": "company",
                        "provider": "workday", "tenant": "acme", "requisition": "REQ-1", "url": ROUTE}),
            (m.applications, {"id": "application", "job_id": "job", "route_id": "route",
                              "profile_id": "profile", "profile_version": "v1", "state": "PREPARED",
                              "target": "MOCK", "fact_ids": [], "trace": {},
                              "created_at": "2026-10-17T00:00:00+00:00", "submitted_at": None}),
        ):
            upsert(connection, table, values)
    yield store, tmp_path, database_url
    store.engine.dispose()


def client_for(store, root):
    return TestClient(create_app(store, "http://127.0.0.1:1", root, operation_mode="FIXTURE"))


def request(action, version, current, *, previous=None, request_id=None):
    payload = {"action": action, "expected_version": version,
               "request_id": request_id or f"checkpoint-{action}-{version}",
               "current_observation": current}
    if previous is not None:
        payload["previous_observation"] = previous
    return payload


def audit_rows(store):
    with store.engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(m.audit).order_by(m.audit.c.id)).mappings()]


def test_lost_response_restart_readback_and_resume_do_not_repeat_navigation(recovery):
    store, root, database_url = recovery
    first = observation("My Information")
    second = observation("My Experience")
    with client_for(store, root) as client:
        initialized = client.post(URL + "/workday", json=request("INITIALIZE", 0, first))
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["decision"] == "INITIALIZED"
        advanced = client.post(URL + "/workday", json=request("NEXT", 1, second, previous=first))
        assert advanced.status_code == 200, advanced.text
        state = advanced.json()["state"]
        assert (state["current_page_id"], state["version"]) == ("my_experience", 2)
    store.engine.dispose()
    # A fresh interpreter must recover the checkpoint solely from the file.
    child = subprocess.run([
        sys.executable, "-c",
        "import json,sys; from pathlib import Path; "
        "from fastapi.testclient import TestClient; "
        "from apps.api.main import create_app; "
        "from packages.persistence.store import Store; "
        "store=Store(sys.argv[1]); "
        "app=create_app(store,'http://127.0.0.1:1',Path(sys.argv[2]),operation_mode='FIXTURE'); "
        "client=TestClient(app); response=client.get(sys.argv[3]); "
        "response.raise_for_status(); print(json.dumps(response.json())); "
        "client.close(); store.engine.dispose()",
        database_url, str(root), URL,
    ], check=True, capture_output=True, text=True, timeout=60)
    assert json.loads(child.stdout) == state
    reopened = Store(database_url)
    try:
        before = audit_rows(reopened)
        with client_for(reopened, root) as client:
            assert client.get(URL).json() == state
            stale_retry = client.post(URL + "/workday", json=request("NEXT", 1, second, previous=first))
            assert stale_retry.status_code == 409
            resumed = client.post(URL + "/workday", json=request("RESUME", 2, second))
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["decision"] == "RESUME_FROM_CONFIRMED_STEP"
            assert resumed.json()["state"] == state
            assert resumed.json()["execute"] is False
            assert client.get(URL).json() == state
        assert audit_rows(reopened) == before
        application = reopened.application("application")
        assert application["state"] == "PREPARED"
        assert application["confirmation"] is None
    finally:
        reopened.engine.dispose()


def test_wrong_route_stale_observation_and_foreign_profile_preserve_checkpoint(recovery):
    store, root, _ = recovery
    first = observation("My Information")
    with client_for(store, root) as client:
        response = client.post(URL + "/workday", json=request("INITIALIZE", 0, first))
        assert response.status_code == 200, response.text
        state = client.get(URL).json()
        before = audit_rows(store)
        for observed in (
            observation("My Information", url=ROUTE.replace("REQ-1", "REQ-2")),
            observation("My Information", stale=True),
            observation("Unknown Page"),
            observation("My Experience"),
        ):
            result = client.post(URL + "/workday", json=request("RESUME", 1, observed))
            assert result.status_code == 200, result.text
            assert result.json()["decision"] != "RESUME_FROM_CONFIRMED_STEP"
            assert result.json()["state"] is None
            assert result.json()["execute"] is False
            assert client.get(URL).json() == state
        foreign = client.post(URL.replace("/profile/", "/other/") + "/workday",
                              json=request("RESUME", 1, first))
        assert foreign.status_code == 404
        assert audit_rows(store) == before


def test_changed_application_route_invalidates_resume_without_exposing_url_secrets(recovery):
    store, root, _ = recovery
    first = observation("My Information", url=ROUTE + "?token=synthetic-secret#synthetic-fragment")
    with client_for(store, root) as client:
        created = client.post(URL + "/workday", json=request("INITIALIZE", 0, first))
        assert created.status_code == 200, created.text
        state = client.get(URL).json()
        before = audit_rows(store)
        persisted = json.dumps(before)
        assert "synthetic-secret" not in persisted
        assert "synthetic-fragment" not in persisted
        with store.engine.begin() as connection:
            connection.execute(m.routes.update().where(m.routes.c.id == "route").values(
                url=ROUTE.replace("REQ-1", "REQ-2")))
        rejected = client.post(URL + "/workday", json=request("RESUME", 1, first))
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["decision"] == "MANUAL_REVIEW"
        assert rejected.json()["state"] is None
        assert client.get(URL).json() == state
        assert audit_rows(store) == before


def test_invalid_payload_or_submission_cannot_create_a_checkpoint(recovery):
    store, root, _ = recovery
    first = observation("My Information")
    with client_for(store, root) as client:
        for payload in (
            request("FINAL_SUBMIT", 0, first),
            {**request("INITIALIZE", 0, first), "progress": {"password": "synthetic"}},
            request("INITIALIZE", True, first),
            request("INITIALIZE", 0, {**first, "cookie": "synthetic"}),
        ):
            rejected = client.post(URL + "/workday", json=payload)
            assert rejected.status_code == 422, rejected.text
        assert client.get(URL).json()["version"] == 0
        assert audit_rows(store) == []


@pytest.mark.parametrize("state", ["SUBMITTING", "CONFIRMED", "SUBMISSION_UNCONFIRMED"])
def test_resume_does_not_certify_an_application_already_in_submission(recovery, state):
    store, root, _ = recovery
    first = observation("My Information")
    with client_for(store, root) as client:
        created = client.post(URL + "/workday", json=request("INITIALIZE", 0, first))
        assert created.status_code == 200, created.text
        before = audit_rows(store)
        with store.engine.begin() as connection:
            connection.execute(m.applications.update().where(m.applications.c.id == "application").values(state=state))
        rejected = client.post(URL + "/workday", json=request("RESUME", 1, first))
        assert rejected.status_code == 409, rejected.text
        assert audit_rows(store) == before


def test_running_api_rejects_changed_workspace_identity(recovery):
    store, root, _ = recovery
    first = observation("My Information")
    with client_for(store, root) as client:
        created = client.post(URL + "/workday", json=request("INITIALIZE", 0, first))
        assert created.status_code == 200, created.text
        before = audit_rows(store)
        with store.engine.begin() as connection:
            connection.execute(m.workspace_identity.update().where(
                m.workspace_identity.c.id == "workspace").values(operation_mode="PERSONAL"))
        rejected = client.post(URL + "/workday", json=request("RESUME", 1, first))
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["detail"]["code"] == "WORKSPACE_MODE_MISMATCH"
        assert audit_rows(store) == before


def test_repointed_route_with_same_url_cannot_reuse_a_checkpoint(recovery):
    store, root, _ = recovery
    first = observation("My Information")
    with client_for(store, root) as client:
        created = client.post(URL + "/workday", json=request("INITIALIZE", 0, first))
        assert created.status_code == 200, created.text
        before = audit_rows(store)
        with store.engine.begin() as connection:
            original = dict(connection.execute(select(m.routes).where(m.routes.c.id == "route")).mappings().one())
            connection.execute(m.routes.insert().values({
                **original, "id": "repointed-route", "requisition": "REQ-1-repointed",
            }))
            connection.execute(m.applications.update().where(m.applications.c.id == "application").values(
                route_id="repointed-route"))
        rejected = client.post(URL + "/workday", json=request("RESUME", 1, first))
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["decision"] == "MANUAL_REVIEW"
        assert rejected.json()["state"] is None
        assert audit_rows(store) == before
