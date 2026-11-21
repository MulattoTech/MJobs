"""MJ-REQ-205 intention APIs, with real durable local fixture execution.

Route names in the original requirement are illustrative. These journeys use the
mounted production API, actual local worker entrypoint and a reopened SQLite
store. They never execute an external collector or submit an application.
"""
import os
from copy import deepcopy
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


def _store(path, mode):
    store = Store(f"sqlite:///{path.as_posix()}")
    store.migrate()
    store.bind_workspace_mode(mode)
    return store


def _complete(store, origin, output, operation_id):
    with patch.dict(os.environ, {
        "MJOBS_DATABASE_URL": store.url,
        "MJOBS_MOCK_ORIGIN": origin,
        "MJOBS_OUTPUT_DIR": str(output),
        "MJOBS_OPERATION_MODE": "FIXTURE",
    }):
        assert execute_operation(operation_id)["status"] == "COMPLETED"


def test_job_save_prepare_application_list_and_retry_survive_reopen(tmp_path):
    """A saved job leads to documents, scoped reads and an explicit safe retry."""
    database = tmp_path / "jobs.db"
    output = tmp_path / "documents"
    store = _store(database, "FIXTURE")
    try:
        with fixture_server() as origin:
            with TestClient(create_app(store, origin, output, operation_mode="FIXTURE")) as client:
                client.put("/profiles", json=deepcopy(PROFILE)).raise_for_status()
                other = deepcopy(PROFILE)
                other.update(id="other", name="Other Fixture", email="other@example.test")
                client.put("/profiles", json=other).raise_for_status()

                admitted = client.post("/discovery", json={
                    "profile_id": "fixture", "request_id": "api-design-discover",
                })
                assert admitted.status_code == 202, admitted.text
                discovery_id = admitted.json()["operation_id"]
                assert client.get(f"/operations/{discovery_id}").json()["state"] == "QUEUED"
                _complete(store, origin, output, discovery_id)
                assert client.get(f"/operations/{discovery_id}").json()["state"] == "COMPLETED"

                listed = client.get("/jobs", params={"profile_id": "fixture"})
                listed.raise_for_status()
                assert len(listed.json()["jobs"]) == 2
                job = next(row for row in listed.json()["jobs"] if row["requisition"] == "B")
                job_id = job["id"]
                detail = client.get(f"/jobs/{job_id}", params={"profile_id": "fixture"})
                detail.raise_for_status()
                assert detail.json()["title"] == job["title"]
                saved = client.put(f"/profiles/fixture/jobs/{job_id}/tracking/disposition", json={
                    "disposition": "SAVED", "request_id": "api-design-save",
                })
                assert saved.status_code == 200, saved.text
                assert saved.json()["disposition"] == "SAVED"

                prepared = client.post("/applications", json={
                    "profile_id": "fixture", "job_id": job_id,
                    "request_id": "api-design-prepare",
                })
                assert prepared.status_code == 202, prepared.text
                application_id = prepared.json()["application_id"]
                preparation_id = prepared.json()["operation_id"]
                _complete(store, origin, output, preparation_id)
                applications = client.get("/applications", params={"profile_id": "fixture"})
                applications.raise_for_status()
                assert [row["id"] for row in applications.json()["applications"]] == [application_id]
                assert client.get("/applications", params={"profile_id": "other"}).json() == {"applications": []}
                application = client.get(f"/profiles/fixture/applications/{application_id}")
                application.raise_for_status()
                assert application.json()["state"] == "PREPARED"
                assert application.json()["profile_id"] == "fixture"
                assert client.get(f"/profiles/other/applications/{application_id}").status_code == 404
                documents = client.get(f"/profiles/fixture/applications/{application_id}/documents")
                documents.raise_for_status()
                resume = next(row for row in documents.json()["documents"] if row["kind"] == "resume")
                assert resume["preview_error"] is None
                download = client.get(f"/profiles/fixture/documents/{resume['id']}/txt")
                download.raise_for_status()
                assert "Python" in download.text
                assert client.get("/settings").json()["external_submission_enabled"] is False

                # Inject a pre-execution failure into a real admitted dry-run
                # intent. The retry endpoint must retain that operation identity
                # and must not turn review into a submission or launch a browser.
                run = client.post(f"/profiles/fixture/applications/{application_id}/mock-run", json={
                    "request_id": "api-design-dry-run", "submit": False,
                })
                assert run.status_code == 202, run.text
                run_id = run.json()["operation_id"]
                with store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == run_id).values(
                        state="DEAD_LETTERED", error="RETRY_LIMIT", attempt_count=3,
                    ))
                with patch("services.applications.mock_adapter.sync_playwright",
                           side_effect=AssertionError("Retry admission opened a browser")):
                    retried = client.post(f"/operations/{run_id}/retry")
                    assert retried.status_code == 202, retried.text
                    assert retried.json() == {"operation_id": run_id, "state": "QUEUED"}
                    assert client.post(f"/operations/{run_id}/retry").status_code == 409
                retry_readback = client.get(f"/operations/{run_id}").json()
                assert retry_readback["params"]["submit"] is False
                assert retry_readback["retry_count"] == 1
                assert retry_readback["attempt_count"] == 3
                assert store.count("application_attempts") == 0

            store.engine.dispose()
            store = _store(database, "FIXTURE")
            with TestClient(create_app(store, origin, output, operation_mode="FIXTURE")) as client:
                assert client.get(f"/profiles/fixture/jobs/{job_id}/tracking").json()["disposition"] == "SAVED"
                assert client.get(f"/operations/{preparation_id}").json()["state"] == "COMPLETED"
                assert [row["id"] for row in client.get(
                    "/applications", params={"profile_id": "fixture"},
                ).json()["applications"]] == [application_id]
                assert client.get("/applications", params={"profile_id": "other"}).json() == {"applications": []}
                assert client.get(f"/profiles/fixture/documents/{resume['id']}/txt").text == download.text
                assert client.get(f"/operations/{run_id}").json()["state"] == "QUEUED"
                assert client.get(f"/profiles/fixture/applications/{application_id}").json()["state"] == "PREPARED"
                assert store.count("application_attempts") == 0
    finally:
        store.engine.dispose()


def test_company_discovery_and_explicit_rediscovery_preserve_company_reads(tmp_path):
    """Company-source reads and explicit rediscovery use stable persisted identity."""
    database = tmp_path / "companies.db"
    store = _store(database, "FIXTURE")
    output = tmp_path / "documents"
    try:
        with fixture_server() as origin:
            with TestClient(create_app(store, origin, output, operation_mode="FIXTURE")) as client:
                client.put("/profiles", json=deepcopy(PROFILE)).raise_for_status()
                candidate = {
                    "company_name": "Example Systems Inc.",
                    "company_website": origin + "/company",
                    "application_url": origin + "/board/apply/A",
                    "profile_id": "fixture",
                }
                first = client.post("/companies/discovery", json={
                    **candidate, "reason": "USER_ADDED_COMPANY", "request_id": "api-company-first",
                })
                assert first.status_code == 202, first.text
                assert first.json()["status"] == "QUEUED"
                first_id = first.json()["operation_id"]
                company_id = first.json()["company_id"]
                _complete(store, origin, output, first_id)

                view = client.get("/company-sources/view", params={"profile_id": "fixture"})
                view.raise_for_status()
                rows = view.json()["company_sources"]
                assert len(rows) == 1
                assert rows[0]["company_id"] == company_id
                assert rows[0]["company_name"] == "Example Systems Inc."
                source_id = rows[0]["id"]
                listed = client.get("/company-sources", params={"company_id": company_id})
                listed.raise_for_status()
                assert [row["id"] for row in listed.json()["company_sources"]] == [source_id]
                job_ids = {row["id"] for row in client.get(
                    "/jobs", params={"profile_id": "fixture"},
                ).json()["jobs"]}
                assert len(job_ids) == 2

                rediscovered = client.post("/companies/discovery", json={
                    **candidate, "reason": "KNOWN_SOURCE_BROKEN", "request_id": "api-company-rediscover",
                })
                assert rediscovered.status_code == 202, rediscovered.text
                assert rediscovered.json()["company_id"] == company_id
                second_id = rediscovered.json()["operation_id"]
                assert second_id != first_id
                operation = client.get(f"/operations/{second_id}").json()
                assert operation["params"]["trigger_reason"] == "KNOWN_SOURCE_BROKEN"
                _complete(store, origin, output, second_id)
                assert client.get(f"/operations/{first_id}").json()["state"] == "COMPLETED"
                assert client.get(f"/operations/{second_id}").json()["state"] == "COMPLETED"
                assert {row["id"] for row in client.get(
                    "/jobs", params={"profile_id": "fixture"},
                ).json()["jobs"]} == job_ids
                assert store.count("company_job_sources") == 1

            store.engine.dispose()
            store = _store(database, "FIXTURE")
            with TestClient(create_app(store, origin, output, operation_mode="FIXTURE")) as client:
                rows = client.get("/company-sources/view", params={"profile_id": "fixture"}).json()["company_sources"]
                assert [(row["id"], row["company_id"]) for row in rows] == [(source_id, company_id)]
                assert client.get(f"/operations/{second_id}").json()["state"] == "COMPLETED"
                assert client.get("/settings").json()["external_submission_enabled"] is False
    finally:
        store.engine.dispose()


def test_personal_source_run_is_a_durable_intent_without_external_execution(tmp_path):
    """The public collection API admits work; it never performs collection inline."""
    database = tmp_path / "sources.db"
    store = _store(database, "PERSONAL")
    output = tmp_path / "documents"
    origin = "http://127.0.0.1:8090"
    try:
        with TestClient(create_app(store, origin, output, operation_mode="PERSONAL")) as client:
            client.put("/profiles", json=deepcopy(PROFILE)).raise_for_status()
            configured = client.put("/sources", json={"provider": "greenhouse", "tenant": "api-design-fixture"})
            configured.raise_for_status()
            source_id = configured.json()["id"]
            assert [row["id"] for row in client.get("/sources").json()["sources"]] == [source_id]
            payload = {"profile_id": "fixture", "request_id": "api-design-source-run"}
            with patch("workers.tasks.collect_source", side_effect=AssertionError("API admission executed collection")):
                admitted = client.post(f"/sources/{source_id}/collect", json=payload)
                assert admitted.status_code == 202, admitted.text
                operation_id = admitted.json()["operation_id"]
                assert client.post(f"/sources/{source_id}/collect", json=payload).json()["operation_id"] == operation_id
            operation = client.get(f"/operations/{operation_id}").json()
            assert operation["state"] == "QUEUED"
            assert operation["params"]["source_id"] == source_id
            assert client.get("/jobs", params={"profile_id": "fixture"}).json()["jobs"] == []
            assert client.get("/settings").json()["external_submission_enabled"] is False
            assert client.post("/discovery", json={"profile_id": "fixture"}).status_code == 409

        store.engine.dispose()
        store = _store(database, "PERSONAL")
        with TestClient(create_app(store, origin, output, operation_mode="PERSONAL")) as client:
            assert [row["id"] for row in client.get("/sources").json()["sources"]] == [source_id]
            assert client.get(f"/operations/{operation_id}").json()["state"] == "QUEUED"
            assert client.get("/jobs", params={"profile_id": "fixture"}).json()["jobs"] == []
    finally:
        store.engine.dispose()
