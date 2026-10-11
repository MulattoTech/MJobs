"""Profile-scoped readiness action API tests using synthetic persisted operations."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import ApplicationInput
from apps.api.pipeline_workbench import create_pipeline_workbench_router
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation


class PipelineWorkbenchActionTests(unittest.TestCase):
    JOB_ID = "f" * 64

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-pipeline-workbench-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'pipeline.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(deepcopy(PROFILE))
        self.item = {
            "job_id": self.JOB_ID,
            "route_id": "synthetic-route-id",
            "stage": "READY_TO_PREPARE",
            "blockers": [],
            "can_prepare": True,
            "fingerprint": "a" * 64,
            "route_mode": "THIS_ROUTE",
        }
        self.snapshot = {
            "profile_id": "fixture",
            "profile_version": "v" * 64,
            "items": [self.item],
            "counts": {"total": 1},
        }
        self.callback_calls = []

        def prepare_callback(values):
            self.callback_calls.append(dict(values))
            ApplicationInput(**values)
            operation_id = enqueue_operation(self.store, "PREPARE", values["profile_id"], {
                "profile_id": values["profile_id"],
                "job_id": values["job_id"],
                "request_id": values["request_id"],
                "profile_version": values["expected_profile_version"],
                "route_mode": values["route_mode"],
                "route_id": values["route_id"],
            })
            with self.store.engine.connect() as connection:
                state = connection.execute(
                    m.operations.select().with_only_columns(m.operations.c.state).where(
                        m.operations.c.id == operation_id,
                    )
                ).scalar_one()
            return {"operation_id": operation_id, "state": state}

        self.app = FastAPI()
        self.app.include_router(create_pipeline_workbench_router(
            self.store, prepare_callback=prepare_callback,
        ))
        self.client = TestClient(self.app)
        self.snapshot_patch = patch(
            "apps.api.pipeline_workbench._read_workbench",
            side_effect=lambda store, profile_id, limit=100: deepcopy(self.snapshot)
            if profile_id == "fixture" else {
                "profile_id": profile_id, "profile_version": "x" * 64,
                "items": [], "counts": {"total": 0},
            },
        )
        self.snapshot_patch.start()

    def tearDown(self):
        self.snapshot_patch.stop()
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _queue(self, **overrides):
        return self.client.post(
            f"/profiles/fixture/pipeline-workbench/{self.JOB_ID}/prepare",
            json={
                "request_id": "synthetic-prepare-001",
                "expected_profile_version": self.snapshot["profile_version"],
                "fingerprint": self.item["fingerprint"],
                **overrides,
            },
        )

    def test_read_route_uses_persisted_core_snapshot_for_profile(self):
        self.snapshot_patch.stop()
        try:
            response = self.client.get("/profiles/fixture/pipeline-workbench")
        finally:
            self.snapshot_patch.start()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["profile_id"], "fixture")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["counts"]["total"], 0)

    def test_fresh_ready_request_queues_once_and_replays_actual_state(self):
        first = self._queue()
        self.assertEqual(first.status_code, 202, first.text)
        payload = first.json()
        self.assertEqual(payload["request_id"], "synthetic-prepare-001")
        self.assertEqual(payload["job_id"], self.JOB_ID)
        self.assertEqual(payload["state"], "QUEUED")
        self.assertEqual(payload["next_action"], "REFRESH_WORKBENCH")
        self.assertTrue(payload["operation_id"])
        self.assertEqual(self.callback_calls, [{
            "profile_id": "fixture",
            "job_id": self.JOB_ID,
            "request_id": "synthetic-prepare-001",
            "expected_profile_version": self.snapshot["profile_version"],
            "route_mode": "THIS_ROUTE",
            "route_id": "synthetic-route-id",
        }])

        replay = self._queue()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json(), payload)
        self.assertEqual(len(self.callback_calls), 1)

    def test_stale_version_or_fingerprint_never_calls_prepare(self):
        for body in (
            {"expected_profile_version": "stale-version"},
            {"fingerprint": "b" * 64},
        ):
            with self.subTest(body=body):
                response = self._queue(**body)
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.callback_calls, [])

    def test_non_ready_item_never_calls_prepare(self):
        self.item["can_prepare"] = False
        response = self._queue()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.callback_calls, [])

    def test_new_application_uses_explicit_selected_route(self):
        self.item["route_mode"] = "DEFAULT"
        response = self._queue()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.callback_calls[0]["route_mode"], "THIS_ROUTE")
        self.assertEqual(self.callback_calls[0]["route_id"], "synthetic-route-id")

    def test_unexpected_route_mode_fails_closed(self):
        self.item["route_mode"] = ["UNSAFE"]
        response = self._queue()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.callback_calls, [])

    def test_request_id_cannot_be_rebound_to_different_readiness(self):
        first = self._queue()
        self.assertEqual(first.status_code, 202, first.text)
        self.item["fingerprint"] = "c" * 64
        rebound = self._queue(fingerprint="c" * 64)
        self.assertEqual(rebound.status_code, 409, rebound.text)
        self.assertEqual(len(self.callback_calls), 1)

    def test_replay_returns_current_persisted_state_without_requeueing(self):
        first = self._queue()
        self.assertEqual(first.status_code, 202, first.text)
        operation_id = first.json()["operation_id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id,
            ).values(state="COMPLETED"))

        replay = self._queue()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["state"], "COMPLETED")
        self.assertEqual(replay.json()["operation_id"], operation_id)
        self.assertEqual(replay.json()["next_action"], "REFRESH_WORKBENCH")
        self.assertEqual(len(self.callback_calls), 1)

    def test_response_never_serializes_callback_payload(self):
        def unsafe_callback(values):
            self.callback_calls.append(dict(values))
            operation_id = enqueue_operation(self.store, "PREPARE", values["profile_id"], {
                "profile_id": values["profile_id"], "job_id": values["job_id"],
                "request_id": values["request_id"],
                "profile_version": values["expected_profile_version"],
                "route_mode": values["route_mode"], "route_id": values["route_id"],
            })
            return {
                "operation_id": operation_id, "state": "QUEUED",
                "raw_params": {"secret": "SYNTHETIC_PRIVATE_PAYLOAD"},
                "error": "SYNTHETIC_RAW_ERROR",
            }

        self.app.router.routes.clear()
        self.app.include_router(create_pipeline_workbench_router(
            self.store, prepare_callback=unsafe_callback,
        ))
        response = self._queue()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotIn("SYNTHETIC_PRIVATE_PAYLOAD", response.text)
        self.assertNotIn("SYNTHETIC_RAW_ERROR", response.text)

    def test_malformed_callback_state_fails_closed_without_raw_exception(self):
        self.app.router.routes.clear()
        self.app.include_router(create_pipeline_workbench_router(
            self.store,
            prepare_callback=lambda values: {
                "operation_id": "synthetic-operation", "state": [],
            },
        ))
        response = self._queue()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {
            "detail": {"code": "PIPELINE_PREPARE_UNAVAILABLE"},
        })


if __name__ == "__main__":
    unittest.main()
