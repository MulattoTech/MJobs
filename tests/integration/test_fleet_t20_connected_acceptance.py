"""Team 20 API-to-durable-store release acceptance, using synthetic data only."""
import gc
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.dispatch import dispatch_pending
from workers.tasks import execute_operation


def _form_review_fixture(root):
    database_url = f"sqlite:///{(root / 'form-review.db').as_posix()}"
    store = Store(database_url)
    store.migrate()
    store.bind_workspace_mode("FIXTURE")
    store.save_profile(dict(PROFILE))
    with fixture_server() as origin:
        discover(store, origin, "fixture")
        job = next(row for row in store.jobs("fixture") if row["requisition"] == "B")
        application_id = prepare(store, job["id"], "fixture", root / "documents")
    application = store.application(application_id)
    route_url = "https://boards.greenhouse.io/example/jobs/12345"
    with store.engine.begin() as connection:
        connection.execute(update(m.routes).where(
            m.routes.c.id == application["route_id"],
        ).values(provider="greenhouse", tenant="example", requisition="12345", url=route_url))
        connection.execute(update(m.applications).where(
            m.applications.c.id == application_id,
        ).values(target="REAL"))
    return database_url, store, application_id, route_url


def _review_client(store, root, *, operation_mode="FIXTURE"):
    return TestClient(create_app(
        store, "http://127.0.0.1:1", root,
        "redis://127.0.0.1:1/0", operation_mode=operation_mode,
    ))


def _execute_form_review(operation_id, database_url, root, public_client):
    with patch.dict("os.environ", {
        "MJOBS_OPERATION_MODE": "FIXTURE",
        "MJOBS_DATABASE_URL": database_url,
        "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
        "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
        "MJOBS_OUTPUT_DIR": str(root / "documents"),
    }), patch("workers.tasks.PublicCompanyClient", public_client):
        return execute_operation(operation_id)


def test_public_company_discovery_intent_survives_queue_outage_and_replay():
    """Exercise the public API, durable outbox, interruption, replay and readback."""
    with tempfile.TemporaryDirectory(prefix="mjobs-t20-company-acceptance-") as directory:
        root = Path(directory)
        database_url = f"sqlite:///{(root / 'acceptance.db').as_posix()}"
        store = Store(database_url)
        store.migrate()
        client = None
        try:
            store.bind_workspace_mode("PERSONAL")
            store.save_profile({**PROFILE, "id": "t20-synthetic"})
            client = TestClient(create_app(
                store,
                "http://127.0.0.1:8090",
                root,
                "redis://127.0.0.1:1/0",
                operation_mode="PERSONAL",
            ))
            intent = {
                "company_name": "Synthetic Example Systems",
                "company_website": "https://employer.example/",
                "reason": "USER_ADDED_COMPANY",
                "profile_id": "t20-synthetic",
                "request_id": "t20-company-intent-1",
            }
            accepted = client.post("/companies/discovery", json=intent)
            assert accepted.status_code == 202, accepted.text
            response = accepted.json()
            assert response["status"] == "QUEUED"
            operation_id = response["operation_id"]

            # A missing Redis service is an interruption. It must leave exactly
            # one retryable, durable operation rather than lose or duplicate intent.
            dispatch = dispatch_pending(store, "redis://127.0.0.1:1/0")
            assert dispatch["status"] == "QUEUE_UNAVAILABLE"
            replay = client.post("/companies/discovery", json=intent)
            assert replay.status_code == 202, replay.text
            assert replay.json()["operation_id"] == operation_id

            # Reusing an idempotency key for a different intent must fail closed.
            conflict = client.post("/companies/discovery", json={
                **intent,
                "company_name": "Different Synthetic Employer",
            })
            assert conflict.status_code == 409

            assert client.get("/settings").json()["external_submission_enabled"] is False
            client.close()
            client = None
            store.engine.dispose()
            store = None
            gc.collect()

            fresh = Store(database_url)
            fresh_client = None
            try:
                fresh_client = _review_client(fresh, root, operation_mode="PERSONAL")
                exact = fresh_client.get(f"/operations/{operation_id}")
                latest = fresh_client.get("/operations?profile_id=t20-synthetic")
                replay_after_restart = fresh_client.post("/companies/discovery", json=intent)
                assert exact.status_code == 200, exact.text
                assert exact.json()["state"] == "QUEUED"
                assert latest.status_code == 200, latest.text
                assert [row["id"] for row in latest.json()["operations"]] == [operation_id]
                assert replay_after_restart.status_code == 202, replay_after_restart.text
                assert replay_after_restart.json()["operation_id"] == operation_id
                with fresh.engine.connect() as connection:
                    operation = connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id,
                    )).mappings().one()
                assert operation["state"] == "QUEUED"
                assert operation["params"]["fetch_mode"] == "PUBLIC_HTTPS"
                assert operation["params"]["request_id"] == intent["request_id"]
                assert fresh.count("worker_operations") == 1
            finally:
                if fresh_client is not None:
                    fresh_client.close()
                fresh.engine.dispose()
        finally:
            if client is not None:
                client.close()
            if store is not None:
                store.engine.dispose()


def test_form_review_survives_full_restart_with_safe_exact_and_latest_readback(tmp_path, request):
    root = tmp_path
    database_url, store, application_id, route_url = _form_review_fixture(root)
    request.addfinalizer(store.engine.dispose)
    client = _review_client(store, root)
    request.addfinalizer(client.close)
    path = f"/profiles/fixture/applications/{application_id}/form-reviews"
    request_id = "t20-restart-review"
    queued = client.post(path, json={"request_id": request_id})
    assert queued.status_code == 202, queued.text
    operation_id = queued.json()["operation_id"]
    private_value = "PRIVATE_T20_FORM_VALUE"
    html = (
        "<form><label for='email'>Email address</label>"
        f"<input id='email' name='email' type='email' required value='{private_value}'>"
        "</form>"
    ).encode()

    class FakeResponse:
        headers = {"content-type": "text/html; charset=utf-8"}
        content = html
        url = route_url

    class SyntheticPublicClient:
        def __init__(self, website, check=None, total_timeout=None):
            self.website = website
            self.check = check

        def get(self, url):
            self.check()
            assert url == route_url
            response = FakeResponse()
            response.url = url
            return response

        def allowed(self, url):
            return url == route_url

        def close(self):
            pass

    result = _execute_form_review(
        operation_id, database_url, root, SyntheticPublicClient,
    )
    assert result["status"] == "COMPLETED"

    client.close()
    store.engine.dispose()
    del client, store
    gc.collect()

    reopened = Store(database_url)
    request.addfinalizer(reopened.engine.dispose)
    reopened_client = _review_client(reopened, root)
    request.addfinalizer(reopened_client.close)
    try:
        exact = reopened_client.get(path + "/" + request_id)
        latest = reopened_client.get(path + "/latest")
        assert exact.status_code == latest.status_code == 200
        assert exact.json()["state"] == latest.json()["state"] == "COMPLETED"
        assert exact.json()["request_id"] == latest.json()["request_id"] == request_id
        assert exact.json()["analysis"]["fields"][0]["name"] == "email"
        assert latest.json()["analysis"] == exact.json()["analysis"]
        assert private_value not in exact.text
        assert private_value not in latest.text
        with reopened.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one()
            audit = connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == application_id,
                m.audit.c.action == "APPLICATION_FORM_REVIEW",
            )).mappings().one()["payload"]
        assert operation["state"] == "COMPLETED"
        assert private_value not in str(operation["params"])
        assert private_value not in str(audit)
    finally:
        reopened_client.close()
        reopened.engine.dispose()


def test_form_review_route_change_during_interruption_dead_letters_without_fetch(tmp_path, request):
    root = tmp_path
    database_url, store, application_id, route_url = _form_review_fixture(root)
    request.addfinalizer(store.engine.dispose)
    client = _review_client(store, root)
    request.addfinalizer(client.close)
    path = f"/profiles/fixture/applications/{application_id}/form-reviews"
    request_id = "t20-route-changed-review"
    queued = client.post(path, json={"request_id": request_id})
    assert queued.status_code == 202, queued.text
    operation_id = queued.json()["operation_id"]

    # Simulate a route edit between durable admission and worker execution.
    changed_url = "https://boards.greenhouse.io/example/jobs/54321"
    route_id = store.application(application_id)["route_id"]
    with store.engine.begin() as connection:
        connection.execute(update(m.routes).where(
            m.routes.c.id == route_id,
        ).values(url=changed_url))
    fetched = []
    private_value = "PRIVATE_T20_UNFETCHED_VALUE"

    class UnexpectedPublicClient:
        def __init__(self, website, check=None, total_timeout=None):
            pass

        def get(self, url):
            fetched.append(url)
            raise AssertionError(private_value)

        def allowed(self, url):
            return url in {route_url, changed_url}

        def close(self):
            pass

    result = _execute_form_review(
        operation_id, database_url, root, UnexpectedPublicClient,
    )
    assert result["status"] == "DEAD_LETTERED"
    assert fetched == []

    client.close()
    store.engine.dispose()
    del client, store
    gc.collect()

    reopened = Store(database_url)
    request.addfinalizer(reopened.engine.dispose)
    reopened_client = _review_client(reopened, root)
    request.addfinalizer(reopened_client.close)
    try:
        exact = reopened_client.get(path + "/" + request_id)
        latest = reopened_client.get(path + "/latest")
        assert exact.status_code == latest.status_code == 200
        assert exact.json()["state"] == "DEAD_LETTERED"
        assert exact.json()["error_code"] == "FORM_REVIEW_INPUT_STALE"
        assert exact.json()["analysis"] is None
        assert latest.json()["state"] == "UNKNOWN"
        assert latest.json()["analysis"] is None
        assert private_value not in exact.text
        assert private_value not in latest.text
        with reopened.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one()
        assert operation["state"] == "DEAD_LETTERED"
        assert operation["error"] == "FORM_REVIEW_INPUT_STALE"
        assert private_value not in str(operation["params"])
    finally:
        reopened_client.close()
        reopened.engine.dispose()
