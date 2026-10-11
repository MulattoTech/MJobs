"""Production composition checks for the second connected feature batch."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class FeatureBatchCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-batch2-wiring-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'data.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.app = create_app(self.store, "http://127.0.0.1:1", self.root,
                              "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_feature_routes_are_registered_with_the_production_app(self):
        required = {
            "/profiles/{profile_id}/applications/{application_id}/rehearsal",
            "/profiles/{profile_id}/applications/{application_id}/rehearsal/reviews",
            "/profile-restores/preview",
            "/profile-restores",
            "/profiles/{profile_id}/pipeline-workbench",
            "/profiles/{profile_id}/pipeline-workbench/{job_id}/prepare",
            "/retention/policy",
            "/profiles/{profile_id}/applications/{application_id}/email-status/preview",
        }
        # FastAPI can retain included routers as nested route objects. Its public
        # schema exposes registered paths independently of that implementation.
        routes = set(self.app.openapi()["paths"])
        self.assertFalse(required - routes, required - routes)
        response = self.client.post("/profile-restores/preview", json={"backup": {}})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "PROFILE_RESTORE_INVALID")

    def test_empty_workbench_is_profile_scoped_real_api_response(self):
        response = self.client.get("/profiles/fixture/pipeline-workbench")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["profile_id"], "fixture")
        self.assertEqual(response.json()["items"], [])
        missing = self.client.get("/profiles/not-a-profile/pipeline-workbench")
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_workbench_uses_real_preparation_validation_and_atomic_replay(self):
        job_id = "a" * 64
        version = self.store.profile("fixture")["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="company", name="Synthetic Employer", domain="example.test"))
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id="company", requisition="REQ-1",
                title="Synthetic Engineer", description="Synthetic local role",
                skills=["python"], location="Remote", remote_type="REMOTE", attributes={}))
            connection.execute(m.matches.insert().values(
                id="match", job_id=job_id, profile_id="fixture", profile_version=version,
                score=88, missing=[], components={"skills": 88}))
            connection.execute(m.routes.insert().values(
                id="route", job_id=job_id, company_id="company", provider="greenhouse",
                tenant="example", requisition="REQ-1", url="https://jobs.example.test/1"))
        snapshot = self.client.get("/profiles/fixture/pipeline-workbench").json()
        self.assertTrue(snapshot["items"][0]["can_prepare"])
        path = f"/profiles/fixture/pipeline-workbench/{job_id}/prepare"
        body = {"request_id": "synthetic-prepare", "expected_profile_version": version,
                "fingerprint": snapshot["items"][0]["fingerprint"]}
        stale = self.client.post(path, json={**body, "expected_profile_version": "stale"})
        self.assertEqual(stale.status_code, 409, stale.text)
        queued = self.client.post(path, json=body)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()["state"], "QUEUED")
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.operations)).mappings().one()
            self.assertEqual(row["entity_id"], "fixture")
            self.assertEqual(row["params"]["job_id"], job_id)
            self.assertEqual(row["params"]["profile_version"], version)
            self.assertTrue(row["params"]["pipeline_workbench_action_fingerprint"])
            connection.execute(m.operations.update().where(
                m.operations.c.id == row["id"]).values(state="COMPLETED"))
        replay = self.client.post(path, json=body)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["operation_id"], queued.json()["operation_id"])
        self.assertEqual(replay.json()["state"], "COMPLETED")
        with self.store.engine.connect() as connection:
            self.assertEqual(len(connection.execute(select(m.operations.c.id)).all()), 1)


if __name__ == "__main__":
    unittest.main()
