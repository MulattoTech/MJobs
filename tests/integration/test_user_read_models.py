"""User-visible read models and safe factual document downloads."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.discovery.sources.collect import configure_source, collect_source
from services.discovery.matching import recalculate
import httpx
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE


class UserReadModelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_profile_job_match_and_application_history_are_connected(self):
        with fixture_server() as origin, TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
            discover(self.store, origin, "fixture")
            self.assertEqual(client.get("/profiles/fixture").json()["facts"][0]["fact_id"], "python")
            jobs = client.get("/jobs", params={"profile_id": "fixture", "q": "data", "remote_type": "REMOTE"}).json()["jobs"]
            self.assertEqual(len(jobs), 1)
            detail = client.get("/jobs/" + jobs[0]["id"], params={"profile_id": "fixture"}).json()
            self.assertEqual(detail["company"], "Example Systems Inc.")
            self.assertEqual(detail["match"]["score"], 100)
            self.assertEqual(detail["match"]["missing"], [])
            self.assertEqual(len(detail["postings"]), 1)
            application = prepare(self.store, jobs[0]["id"], "fixture", self.root)
            history = client.get("/applications", params={"profile_id": "fixture"}).json()["applications"]
            self.assertEqual(history[0]["id"], application)
            self.assertEqual(history[0]["target"], "MOCK")
            scoped = f"/profiles/fixture/applications/{application}"
            self.assertEqual(client.get(scoped).json()["profile_id"], "fixture")
            self.store.save_profile(dict(PROFILE, id="other"))
            self.assertEqual(client.get(f"/profiles/other/applications/{application}").status_code, 404)
            self.assertEqual(client.get(f"/applications/{application}").status_code, 404)

    def test_integrity_checked_downloads_and_traversal_rejection(self):
        with fixture_server() as origin, TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
            discover(self.store, origin, "fixture")
            application = prepare(self.store, self.store.jobs()[0]["id"], "fixture", self.root)
            scoped = "/profiles/fixture/applications/" + application
            documents = client.get(scoped + "/documents").json()["documents"]
            self.assertEqual(len(documents), 2)
            self.store.save_profile(dict(PROFILE, id="other"))
            self.assertEqual(client.get("/profiles/other/applications/" + application + "/documents").status_code, 404)
            self.assertEqual(client.get("/applications/" + application + "/documents").status_code, 404)
            identifier = next(row["id"] for row in documents if row["kind"] == "resume")
            pdf = client.get("/profiles/fixture/documents/" + identifier + "/pdf")
            self.assertEqual(pdf.status_code, 200)
            self.assertTrue(pdf.content.startswith(b"%PDF"))
            self.store.save_profile(dict(PROFILE, id="other-profile"))
            self.assertEqual(client.get("/profiles/other-profile/documents/" + identifier + "/pdf").status_code, 404)
            self.assertEqual(client.get("/documents/" + identifier + "/pdf").status_code, 404)
            self.assertEqual(client.get("/profiles/fixture/documents/" + identifier + "/exe").status_code, 422)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.documents).where(m.documents.c.id == identifier).values(files={"pdf": "../../outside.pdf"}))
            self.assertEqual(client.get("/profiles/fixture/documents/" + identifier + "/pdf").status_code, 409)

    def test_mixed_pending_facts_are_excluded_and_revision_does_not_rewrite_history(self):
        profile = dict(PROFILE, facts=[PROFILE["facts"][0], dict(PROFILE["facts"][1], user_approved=False)])
        old_version = self.store.save_profile(profile)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = self.store.jobs()[0]
            application = prepare(self.store, job["id"], "fixture", self.root)
            self.assertEqual(self.store.application(application)["fact_ids"], ["python"])
            self.store.save_profile(PROFILE)
            prepare(self.store, job["id"], "fixture", self.root)
            with self.assertRaisesRegex(ValueError, "Profile changed"):
                prepare(self.store, job["id"], "fixture", self.root, profile_version=old_version)
        self.assertEqual(self.store.application(application)["fact_ids"], ["python", "sql"])
        self.assertEqual(self.store.application(application)["document_version"], 2)
        with self.store.engine.connect() as connection:
            documents = list(connection.execute(select(m.documents).where(m.documents.c.application_id == application)).mappings())
        self.assertEqual(len(documents), 4)
        self.assertEqual({tuple(row["fact_ids"]) for row in documents}, {("python",), ("python", "sql")})

    def test_live_preparation_is_manual_only_and_profile_edits_rescore_without_fetch(self):
        source = configure_source(self.store, "example")
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"jobs": [RAW]}) if request.url.path.endswith("/jobs") else httpx.Response(200, json={"name": "Example"}))
        collect_source(self.store, source, "fixture", transport=transport)
        job = self.store.jobs("fixture")[0]
        application = prepare(self.store, job["id"], "fixture", self.root)
        self.assertEqual(self.store.application(application)["target"], "REAL")
        with fixture_server() as origin, TestClient(create_app(self.store, origin, self.root, operation_mode="FIXTURE")) as client:
            self.assertEqual(client.post("/profiles/fixture/applications/" + application + "/mock-run", json={"submit": True}).status_code, 409)
            self.assertEqual(client.get("/profiles/fixture", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.store.save_profile(dict(PROFILE, facts=[dict(row, user_approved=False) for row in PROFILE["facts"]]))
        recalculate(self.store, "fixture")
        self.assertEqual(self.store.jobs("fixture")[0]["match_score"], 0)
        self.assertEqual(self.store.application(application)["fact_ids"], ["python", "sql"])
