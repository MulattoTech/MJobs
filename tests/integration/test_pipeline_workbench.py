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
from services.operations.pipeline_workbench import read_pipeline_workbench
from tests.integration.test_spine import PROFILE
from workers.dispatch import enqueue_operation


class PipelineWorkbenchActionTests(unittest.TestCase):
    JOB_ID = "f" * 64

    def test_scoring_admission_replays_terminal_state_and_excludes_preparation(self):
        from apps.api.main import create_app
        self._seed_ready_job()
        self.snapshot_patch.stop()
        try:
            with TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                      "redis://127.0.0.1:1/0", operation_mode="FIXTURE")) as client:
                before = client.get("/profiles/fixture/pipeline-workbench").json()
                request = {"profile_id": "fixture", "request_id": "profile-score-retry"}
                first = client.post("/matches", json=request)
                self.assertEqual(first.status_code, 202, first.text)
                operation_id = first.json()["operation_id"]
                stale = client.post(f"/profiles/fixture/pipeline-workbench/{self.JOB_ID}/prepare", json={
                    "request_id": "prepare-during-score", "expected_profile_version": before["profile_version"],
                    "fingerprint": before["items"][0]["fingerprint"],
                })
                self.assertEqual(stale.status_code, 409, stale.text)
                direct = client.post("/applications", json={"profile_id": "fixture", "job_id": self.JOB_ID,
                    "request_id": "direct-during-score"})
                self.assertEqual(direct.status_code, 409, direct.text)
                self.assertEqual(direct.json()["detail"]["code"], "PROFILE_SCORING_IN_PROGRESS")
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == operation_id).values(state="COMPLETED"))
                changed = deepcopy(PROFILE)
                changed["name"] = "Changed Synthetic Profile"
                self.store.save_profile(changed, expected_version=before["profile_version"])
                replay = client.post("/matches", json=request)
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertEqual(replay.json(), {"operation_id": operation_id, "state": "COMPLETED"})
                prepare_id = enqueue_operation(self.store, "PREPARE", "fixture", {"job_id": self.JOB_ID})
                refused = client.post("/matches", json={**request, "request_id": "new-during-prepare"})
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(refused.json()["detail"]["code"], "PROFILE_PREPARATION_IN_PROGRESS")
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.operations).where(m.operations.c.id == prepare_id).values(state="COMPLETED"))
                fresh = client.post("/matches", json={**request, "request_id": "new-during-prepare"})
                self.assertEqual(fresh.status_code, 202, fresh.text)
                self.assertNotEqual(fresh.json()["operation_id"], operation_id)
        finally:
            self.snapshot_patch.start()

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

        def prepare_callback(values, connection):
            self.callback_calls.append(dict(values))
            ApplicationInput(**values)
            operation_id = enqueue_operation(self.store, "PREPARE", values["profile_id"], {
                "job_id": values["job_id"],
                "request_id": values["request_id"],
                "profile_version": values["expected_profile_version"],
                "route_mode": values["route_mode"],
                "route_id": values["route_id"],
            }, connection=connection)
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

    def test_read_route_hides_missing_profile_as_not_found(self):
        self.snapshot_patch.stop()
        try:
            response = self.client.get("/profiles/missing-profile/pipeline-workbench")
        finally:
            self.snapshot_patch.start()
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), {
            "detail": {"code": "PIPELINE_PROFILE_NOT_FOUND"},
        })

    def _seed_ready_job(self):
        profile_version = self.store.profile("fixture")["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="pipeline-company", name="Synthetic Pipeline Company",
                domain="pipeline.example.test",
            ))
            connection.execute(m.jobs.insert().values(
                id=self.JOB_ID, company_id="pipeline-company",
                requisition="PIPELINE-001", title="Synthetic Platform Engineer",
                description="A synthetic role for local acceptance coverage.",
                skills=["python"], location="Remote", remote_type="REMOTE",
                attributes={},
            ))
            connection.execute(m.matches.insert().values(
                id="pipeline-match", job_id=self.JOB_ID, profile_id="fixture",
                profile_version=profile_version, score=87, missing=[],
                components={"skills": 87},
            ))
            connection.execute(m.routes.insert().values(
                id="pipeline-route", job_id=self.JOB_ID, company_id="pipeline-company",
                provider="greenhouse", tenant="synthetic", requisition="PIPELINE-001",
                url="https://jobs.example.test/PIPELINE-001?private=synthetic",
            ))

    def test_persisted_ready_job_queues_through_real_workbench_projection(self):
        self._seed_ready_job()
        self.snapshot_patch.stop()
        try:
            snapshot = read_pipeline_workbench(self.store, "fixture")
            item = snapshot["items"][0]
            self.assertTrue(item["can_prepare"])
            self.assertEqual(item["route_id"], "pipeline-route")
            self.snapshot = snapshot
            self.item = item
            response = self._queue(request_id="synthetic-real-projection")
        finally:
            self.snapshot_patch.start()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "QUEUED")
        self.assertEqual(self.callback_calls[0]["route_id"], "pipeline-route")
        self.assertNotIn("private=synthetic", response.text)

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

    def test_initial_ack_reads_state_from_persisted_operation(self):
        def already_started(values, connection):
            operation_id = enqueue_operation(self.store, "PREPARE", values["profile_id"], {
                "job_id": values["job_id"],
                "request_id": values["request_id"],
                "profile_version": values["expected_profile_version"],
                "route_mode": values["route_mode"], "route_id": values["route_id"],
            }, connection=connection)
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id,
            ).values(state="RUNNING"))
            return {"operation_id": operation_id, "state": "QUEUED"}

        self.app.router.routes.clear()
        self.app.include_router(create_pipeline_workbench_router(
            self.store, prepare_callback=already_started,
        ))
        response = self._queue()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "RUNNING")

    def test_response_never_serializes_callback_payload(self):
        def unsafe_callback(values, connection):
            self.callback_calls.append(dict(values))
            operation_id = enqueue_operation(self.store, "PREPARE", values["profile_id"], {
                "job_id": values["job_id"],
                "request_id": values["request_id"],
                "profile_version": values["expected_profile_version"],
                "route_mode": values["route_mode"], "route_id": values["route_id"],
            }, connection=connection)
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

    def test_malformed_callback_rolls_back_queued_operation_and_binding(self):
        def invalid_ack(values, connection):
            operation_id = enqueue_operation(self.store, "PREPARE", values["profile_id"], {
                "job_id": values["job_id"],
                "request_id": values["request_id"],
                "profile_version": values["expected_profile_version"],
                "route_mode": values["route_mode"], "route_id": values["route_id"],
            }, connection=connection)
            self.assertTrue(operation_id)
            return {"operation_id": operation_id, "state": []}

        self.app.router.routes.clear()
        self.app.include_router(create_pipeline_workbench_router(
            self.store, prepare_callback=invalid_ack,
        ))
        response = self._queue()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {
            "detail": {"code": "PIPELINE_PREPARE_UNAVAILABLE"},
        })
        with self.store.engine.connect() as connection:
            count = connection.execute(
                m.operations.select().with_only_columns(m.operations.c.id).where(
                    m.operations.c.kind == "PREPARE",
                )
            ).all()
        self.assertEqual(count, [])


if __name__ == "__main__":
    unittest.main()
