"""Profile/job-scoped read-only role-lens selection preview API."""
import copy
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class RoleLensApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mjobs-role-lens-api-")
        self.root = Path(self.temporary.name)
        self.store = Store(f"sqlite:///{(self.root / 'role-lens-api.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))
        self.app = FastAPI()
        from apps.api.role_lenses import create_role_lenses_router
        self.app.include_router(create_role_lenses_router(self.store))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.store.engine.dispose()
        self.temporary.cleanup()

    def _job(self, origin):
        discover(self.store, origin, "fixture")
        return next(row for row in self.store.jobs("fixture") if row["requisition"] == "B")

    def test_default_and_explicit_lens_previews_are_profile_job_scoped_and_read_only(self):
        with fixture_server() as origin:
            job = self._job(origin)
            profile_version = self.store.profile("fixture")["version"]
            facts_before = self.store.profile("fixture")["facts"]
            response = self.client.get(
                f"/profiles/fixture/jobs/{job['id']}/role-lenses/selection"
            )
            explicit = self.client.get(
                f"/profiles/fixture/jobs/{job['id']}/role-lenses/selection",
                params={"role_lens_id": "systems", "expected_role_lens_version": 1},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(explicit.status_code, 200, explicit.text)
        self.assertEqual(response.json()["profile_id"], "fixture")
        self.assertEqual(response.json()["job_id"], job["id"])
        self.assertEqual(response.json()["profile_version"], profile_version)
        self.assertTrue(response.json()["job_fingerprint"])
        self.assertEqual(explicit.json()["selection"]["lens"]["id"], "systems")
        self.assertEqual(explicit.json()["selection"]["lens"]["version"], 1)
        self.assertTrue(explicit.json()["selection"]["lens"]["display_name"])
        self.assertTrue(explicit.json()["role_lenses"])
        self.assertEqual(self.store.profile("fixture")["facts"], facts_before)

    def test_invalid_or_stale_explicit_lens_fails_with_stable_http_error(self):
        with fixture_server() as origin:
            job = self._job(origin)
            base = f"/profiles/fixture/jobs/{job['id']}/role-lenses/selection"
            unknown = self.client.get(base, params={
                "role_lens_id": "invented", "expected_role_lens_version": 1,
            })
            stale = self.client.get(base, params={
                "role_lens_id": "systems", "expected_role_lens_version": 999,
            })
            incomplete = self.client.get(base, params={"role_lens_id": "systems"})

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["detail"]["code"], "ROLE_LENS_NOT_FOUND")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "ROLE_LENS_VERSION_CONFLICT")
        self.assertEqual(incomplete.status_code, 422)
        self.assertEqual(incomplete.json()["detail"]["code"], "ROLE_LENS_VERSION_INVALID")

    def test_profile_and_job_must_exist_for_selection_preview(self):
        missing_profile = self.client.get(
            "/profiles/missing/jobs/unknown/role-lenses/selection"
        )
        self.assertEqual(missing_profile.status_code, 404)
        self.assertEqual(missing_profile.json()["detail"]["code"], "ROLE_LENS_PROFILE_NOT_FOUND")

        self.store.save_profile(copy.deepcopy(PROFILE))
        missing_job = self.client.get(
            "/profiles/fixture/jobs/unknown/role-lenses/selection"
        )
        self.assertEqual(missing_job.status_code, 404)
        self.assertEqual(missing_job.json()["detail"]["code"], "ROLE_LENS_JOB_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
