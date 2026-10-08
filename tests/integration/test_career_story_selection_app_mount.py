"""Verify job-specific story selection is available through the full API app."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from tests.integration.test_spine import PROFILE


class CareerStorySelectionAppMountTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-story-selection-mount-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'selection.sqlite').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "story-mount-company", "name": "Synthetic Systems",
                "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": "story-mount-job", "company_id": "story-mount-company",
                "requisition": "story-mount-job", "title": "Python Support Engineer",
                "description": "Synthetic local test job.", "skills": ["Python"],
                "location": "Remote", "remote_type": "REMOTE", "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "story-mount-route", "job_id": "story-mount-job",
                "company_id": "story-mount-company", "provider": "fixture",
                "tenant": "story-mount", "requisition": "story-mount-job",
                "url": "https://jobs.example.test/story-mount",
            })
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:8090", self.root,
            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_story_selection_route_is_mounted_in_main_app(self):
        response = self.client.get(
            "/profiles/fixture/jobs/story-mount-job/career-stories/selectable",
            params={"allowed_use": "RESUME"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["profile_id"], "fixture")
        self.assertEqual(payload["job_id"], "story-mount-job")
        self.assertEqual(payload["allowed_use"], "RESUME")
        self.assertEqual(payload["stories"], [])
