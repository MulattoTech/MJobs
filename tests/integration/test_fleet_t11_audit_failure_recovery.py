"""Real public PREPARE failure/retry produces durable, value-free audit history."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


PRIVATE_ERROR_CANARY = "password=PRIVATE-AUDIT-ERROR-CANARY"
DOCUMENT_TYPES = {"RESUME_GENERATED", "COVER_LETTER_GENERATED"}


def exercise_public_prepare_failure_recovery(client, store, root, job_id):
    """Same public routes for TestClient preflight and the real browser API server."""
    root = Path(root)
    blocked = root / PRIVATE_ERROR_CANARY
    blocked.write_text("Owned synthetic file; cannot be an output directory.", encoding="utf-8")
    payload = {"profile_id": "fixture", "job_id": job_id,
               "request_id": "audit-private-retry-request"}
    history_path = "/profiles/fixture/audit-history"
    before = client.get(history_path).json()["events"]
    before_ids = {row["id"] for row in before}
    admitted = client.post("/applications", json=payload)
    assert admitted.status_code == 202, admitted.text
    operation_id = admitted.json()["operation_id"]
    env = {"MJOBS_DATABASE_URL": store.url, "MJOBS_OPERATION_MODE": "FIXTURE",
           "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0", "MJOBS_OUTPUT_DIR": str(blocked),
           "MJOBS_OPERATION_TIMEOUT": "180"}
    with patch.dict(os.environ, env):
        failed = execute_operation(operation_id)
        assert failed["status"] == "DEAD_LETTERED", failed
        assert failed["error_category"] in {"NotADirectoryError", "FileExistsError"}, failed
        assert execute_operation(operation_id)["status"] == "ALREADY_CLAIMED_OR_CAPACITY"
    with store.engine.connect() as connection:
        saved = connection.execute(select(m.operations).where(
            m.operations.c.id == operation_id)).mappings().one()
        assert saved["state"] == "DEAD_LETTERED" and saved["dead_lettered_at"]
        assert saved["error"] == failed["error_category"]
        assert PRIVATE_ERROR_CANARY not in str(saved["error"])
    response = client.get(history_path)
    events = response.json()["events"]
    failure_ids = [row["id"] for row in events if row["type"] == "APPLICATION_FAILED"
                   and row["id"] not in before_ids]
    assert len(failure_ids) == 1, events
    assert PRIVATE_ERROR_CANARY not in response.text
    assert "audit-private-retry-request" not in response.text
    replay = client.post("/applications", json=payload)
    assert replay.status_code == 202 and replay.json()["operation_id"] == operation_id, replay.text
    assert [row["id"] for row in client.get(history_path).json()["events"]
            if row["type"] == "APPLICATION_FAILED" and row["id"] not in before_ids] == failure_ids
    retry = client.post("/operations/" + operation_id + "/retry")
    assert retry.status_code == 202 and retry.json()["operation_id"] == operation_id, retry.text
    with patch.dict(os.environ, {**env, "MJOBS_OUTPUT_DIR": str(root)}):
        recovered = execute_operation(operation_id)
        assert recovered["status"] == "COMPLETED", recovered
        assert execute_operation(operation_id)["status"] == "ALREADY_CLAIMED_OR_CAPACITY"
    final_response = client.get(history_path)
    final_events = final_response.json()["events"]
    assert [row["id"] for row in final_events if row["type"] == "APPLICATION_FAILED"
            and row["id"] not in before_ids] == failure_ids
    generated = [row for row in final_events if row["id"] not in before_ids
                 and row["type"] in DOCUMENT_TYPES]
    assert sorted(row["type"] for row in generated) == sorted(DOCUMENT_TYPES), generated
    final_ids = [row["id"] for row in final_events]
    assert len(final_ids) == len(set(final_ids))
    assert [row["id"] for row in client.get(history_path).json()["events"]] == final_ids
    for value in [PRIVATE_ERROR_CANARY, str(blocked), "audit-private-retry-request"]:
        assert value not in final_response.text
    other = client.get("/profiles/other/audit-history")
    assert other.status_code == 200, other.text
    assert not set(failure_ids + [row["id"] for row in generated]) & {
        row["id"] for row in other.json()["events"]}
    application_id = admitted.json()["application_id"]
    assert client.get("/profiles/fixture/applications/" + application_id).status_code == 200
    documents = client.get("/profiles/fixture/applications/" + application_id + "/documents").json()["documents"]
    assert {row["kind"] for row in documents} == {"resume", "cover-letter"}
    for document in documents:
        for extension in ("txt", "docx", "pdf"):
            response = client.get("/profiles/fixture/documents/" + document["id"] + "/" + extension)
            assert response.status_code == 200 and response.content
            assert hashlib.sha256(response.content).hexdigest() == document["sha256"][extension]
    return {"operation_id": operation_id, "application_id": application_id,
            "failure_ids": failure_ids, "generated_ids": [row["id"] for row in generated],
            "history_ids": final_ids}


class AuditFailureRecoveryTests(unittest.TestCase):
    def test_public_prepare_failure_retry_and_fresh_api_history_are_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-t11-audit-") as directory:
            root = Path(directory)
            url = "sqlite:///" + (root / "audit.db").as_posix()
            store = Store(url)
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(PROFILE)
            store.save_profile({**PROFILE, "id": "other", "name": "Other Synthetic Applicant",
                                "email": "other@example.invalid"})
            try:
                with fixture_server() as origin:
                    discover(store, origin, "fixture")
                with TestClient(create_app(store, "http://127.0.0.1:1", root,
                                          "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
                    result = exercise_public_prepare_failure_recovery(
                        client, store, root, store.jobs("fixture")[0]["id"])
            finally:
                store.engine.dispose()
            reopened = Store(url)
            try:
                with TestClient(create_app(reopened, "http://127.0.0.1:1", root,
                                          "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
                    response = client.get("/profiles/fixture/audit-history")
                    self.assertEqual([row["id"] for row in response.json()["events"]], result["history_ids"])
                    self.assertNotIn(PRIVATE_ERROR_CANARY, response.text)
                    self.assertFalse(set(result["failure_ids"] + result["generated_ids"]) & {
                        row["id"] for row in client.get("/profiles/other/audit-history").json()["events"]})
            finally:
                reopened.engine.dispose()
