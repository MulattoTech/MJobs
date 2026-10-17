"""Actual DISCOVER admission, lease recovery, worker retry and public readback."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.operations import OperationCancelled, check_cancelled, claim, recover_expired
from workers.tasks import execute_operation


class RecoveryProgressFixture:
    """Shared synthetic public flow; never requires Redis or an employer page."""

    def __init__(self, root):
        self.root = root
        self.database_url = f"sqlite:///{(root / 'recovery.db').as_posix()}"
        self.environment = patch.dict("os.environ", {
            "MJOBS_DATABASE_URL": self.database_url, "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_OUTPUT_DIR": str(root), "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OPERATION_TIMEOUT": "180",
        })

    def __enter__(self):
        self.environment.start()
        self.store = Store(self.database_url)
        try:
            self.store.migrate()
            self.store.bind_workspace_mode("FIXTURE")
            self.client = self.new_client()
            for profile in [PROFILE, {**PROFILE, "id": "other", "name": "Other fixture",
                                      "email": "other@example.test"}]:
                response = self.client.put("/profiles", json=profile)
                assert response.status_code == 200, response.text
            self.resume_id = self.queue("fixture", "recovered", "PRIVATE_PARAMS_SENTINEL")
            self.other_id = self.queue("other", "foreign", "PRIVATE_FOREIGN_SENTINEL")
            # Both admission requests are finished before durable worker ownership
            # and recovery. The original HTTP client is deliberately gone.
            self.client.close()
            self.original_owner = claim(self.store, self.resume_id)
            assert self.original_owner is not None
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == self.resume_id).values(
                        lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
            assert recover_expired(self.store) == 1
            with self.store.engine.begin() as connection:
                row = connection.execute(select(m.operations).where(
                    m.operations.c.id == self.resume_id)).mappings().one()
                assert row["state"] == "QUEUED" and row["recovery_metadata"] == {"action": "RESUME"}
                connection.execute(update(m.operations).where(
                    m.operations.c.id == self.resume_id).values(
                        error="PRIVATE_ERROR_SENTINEL",
                        recovery_metadata={**row["recovery_metadata"],
                                           "manual_auth_owner": {"token": "PRIVATE_OWNER_SENTINEL"}}))
            self.reopen()
            self.retry_id = self.queue("fixture", "transport-retry")
            self.client.close()
            # Only the local transport boundary fails. The real DISCOVER handler,
            # worker finalization and discovery_retry_plan produce the composite.
            with patch("services.discovery.pipeline.MockClient.get",
                       side_effect=httpx.ReadTimeout("PRIVATE_TRANSPORT_SENTINEL")):
                self.retry_result = execute_operation(self.retry_id)
            assert self.retry_result["status"] == "QUEUED", self.retry_result
            self.reopen()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def new_client(self):
        return TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                    "redis://127.0.0.1:1/0", operation_mode="FIXTURE"))

    def reopen(self):
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = self.new_client()

    def queue(self, profile_id, request_id, query=None):
        payload = {"profile_id": profile_id, "request_id": request_id}
        if query is not None:
            payload["query"] = query
        response = self.client.post("/discovery", json=payload)
        assert response.status_code == 202, response.text
        return response.json()["operation_id"]

    def read(self, profile_id="fixture"):
        response = self.client.get(f"/profiles/{profile_id}/operations/progress")
        assert response.status_code == 200, response.text
        return {row["operation_id"]: row for row in response.json()["operations"]}

    def __exit__(self, *_args):
        if hasattr(self, "client"):
            self.client.close()
        self.store.engine.dispose()
        self.environment.stop()


def test_real_recovery_and_worker_retry_project_durable_vocabulary_after_reopen(tmp_path):
    with RecoveryProgressFixture(tmp_path) as flow:
        rows = flow.read()
        resumed = rows[flow.resume_id]
        retry = rows[flow.retry_id]
        assert resumed["recovery_summary"] == "RESUME"
        assert resumed["state"] == "QUEUED" and resumed["current_step"] == "RECOVERING"
        assert resumed["completed_at"] is None and resumed["progress"] < 100
        assert resumed["retry_count"] == 1
        assert retry["recovery_summary"] == "RETRY: NETWORK_ERROR"
        assert retry["state"] == "QUEUED" and retry["current_step"] == "RETRY_WAIT"
        assert retry["completed_at"] is None and retry["next_attempt_at"] is not None
        assert retry["retry_count"] == 1
        flow.reopen()
        assert flow.read() == rows
        assert recover_expired(flow.store) == 0
        assert flow.read() == rows
        assert flow.store.count("worker_operations") == 3


def test_public_projection_excludes_private_payloads_and_expired_owner_cannot_continue(tmp_path):
    with RecoveryProgressFixture(tmp_path) as flow:
        rows = flow.read()
        assert set(rows) == {flow.resume_id, flow.retry_id}
        serialized = json.dumps(rows)
        for field in ["params", "error", "recovery_metadata", "manual_auth_owner"]:
            assert field not in rows[flow.resume_id]
        for canary in ["PRIVATE_PARAMS_SENTINEL", "PRIVATE_ERROR_SENTINEL", "PRIVATE_OWNER_SENTINEL",
                       "PRIVATE_FOREIGN_SENTINEL", "PRIVATE_TRANSPORT_SENTINEL"]:
            assert canary not in serialized
        assert set(flow.read("other")) == {flow.other_id}
        assert flow.client.get("/profiles/missing/operations/progress").status_code == 404
        try:
            check_cancelled(flow.store, flow.resume_id, flow.original_owner["attempt_count"])
        except OperationCancelled:
            pass
        else:
            raise AssertionError("The expired worker must not continue a queued recovery")
        assert flow.read() == rows
