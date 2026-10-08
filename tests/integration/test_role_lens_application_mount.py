"""The role-lens preview is reachable from the connected application API."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class RoleLensApplicationMountTests(unittest.TestCase):
    def test_main_application_exposes_preview_and_queues_only_confirmed_lens_identity(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-role-lens-mount-") as directory:
            root = Path(directory)
            store = Store(f"sqlite:///{(root / 'app.sqlite').as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(PROFILE)
            with fixture_server() as mock_origin:
                discover(store, mock_origin, "fixture")
                jobs = store.jobs("fixture")
                job = jobs[0]
                app = create_app(
                    store, mock_origin, root,
                    operation_mode="FIXTURE",
                )
                try:
                    with TestClient(app) as client:
                        preview = client.get(
                            f"/profiles/fixture/jobs/{job['id']}/role-lenses/selection"
                        )
                        self.assertEqual(preview.status_code, 200, preview.text)
                        self.assertEqual(preview.json()["profile_id"], "fixture")
                        self.assertEqual(preview.json()["job_id"], job["id"])
                        self.assertIn(
                            preview.json()["selection"]["lens"]["id"],
                            {lens["id"] for lens in preview.json()["role_lenses"]},
                        )

                        queued = client.post("/applications", json={
                            "profile_id": "fixture",
                            "job_id": job["id"],
                            "request_id": "role-lens-request",
                            "role_lens_id": "systems",
                            "expected_role_lens_version": 1,
                        })
                        self.assertEqual(queued.status_code, 202, queued.text)
                        operation_id = queued.json()["operation_id"]
                        with store.engine.connect() as connection:
                            params = connection.execute(
                                select(m.operations.c.params).where(
                                    m.operations.c.id == operation_id,
                                )
                            ).scalar_one()
                        self.assertEqual(params["role_lens_id"], "systems")
                        self.assertEqual(params["expected_role_lens_version"], 1)
                        self.assertNotIn("role_lens_text", params)
                        self.assertNotIn("facts", params)

                        incomplete = client.post("/applications", json={
                            "profile_id": "fixture",
                            "job_id": job["id"],
                            "request_id": "role-lens-unpaired",
                            "role_lens_id": "systems",
                        })
                        self.assertEqual(incomplete.status_code, 422)
                finally:
                    store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
