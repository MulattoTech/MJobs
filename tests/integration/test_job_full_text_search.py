"""Connected API acceptance for the searchable fields in the Jobs view."""
from pathlib import Path
import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class JobFullTextSearchApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-full-text-search-api-")
        self.root = Path(self.directory.name)
        self.database_url = os.environ.get("MJOBS_FULL_TEXT_SEARCH_TEST_DATABASE_URL") or (
            f"sqlite:///{(self.root / 'search.db').as_posix()}")
        self.store = Store(self.database_url)
        if self.store.engine.dialect.name == "sqlite":
            self.store.migrate()
        elif self.store.engine.dialect.name != "postgresql":
            self.fail("Full-text search acceptance requires SQLite or PostgreSQL")
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")

        job = self.store.jobs(profile_id="fixture")[0]
        self.job_id = job["id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(
                title="UniqueRoleSignal",
                description="UniqueDescriptionSignal",
                skills=["UniqueSkillSignal"],
                location="UniqueLocationSignal",
            ))
            connection.execute(update(m.companies).where(
                m.companies.c.id == job["company_id"]).values(name="UniqueCompanySignal"))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_jobs_api_searches_title_company_description_skills_and_location(self):
        app = create_app(self.store, "http://127.0.0.1:1", self.root,
                         "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        with TestClient(app) as client:
            for field, query in (
                    ("title", "UniqueRoleSignal"),
                    ("company", "UniqueCompanySignal"),
                    ("description", "UniqueDescriptionSignal"),
                    ("skills", "UniqueSkillSignal"),
                    ("location", "UniqueLocationSignal")):
                with self.subTest(field=field):
                    response = client.get("/jobs", params={"q": query})
                    self.assertEqual(response.status_code, 200)
                    identifiers = [job["id"] for job in response.json()["jobs"]]
                    self.assertIn(self.job_id, identifiers)
                    if field != "company":
                        self.assertEqual(identifiers, [self.job_id])


if __name__ == "__main__":
    unittest.main()
