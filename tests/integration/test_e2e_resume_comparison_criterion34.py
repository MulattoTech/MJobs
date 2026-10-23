"""Connected synthetic acceptance for MJ-REQ-292 criterion 34."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration.test_spine import PROFILE


class ResumeComparisonCriterion34Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        source = PROFILE["facts"][0]
        facts = [
            dict(source, id="summary", kind="summary", text="Systems administrator."),
            dict(source, id="python", kind="skill", text="Python"),
            dict(source, id="python-work", kind="responsibility",
                 text="Built Python APIs for internal teams."),
            dict(source, id="vmware-work", kind="responsibility",
                 text="Managed VMware hosts for regional offices."),
            dict(source, id="unapproved-python", kind="responsibility",
                 text="Led Python operations for 20 teams.", verified=False,
                 user_approved=False),
        ]
        self.store.save_profile(dict(PROFILE, facts=facts))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_generated_tailored_resume_compares_to_base_after_store_reopen(self):
        # This is an entirely local fixture flow; no employer application or
        # external submission is attempted.
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = self.store.jobs()[0]
            with self.store.engine.begin() as connection:
                connection.execute(
                    update(m.jobs).where(m.jobs.c.id == job["id"]).values(skills=["Python"])
                )

            application_id = prepare(self.store, job["id"], "fixture", self.root)
            base_rows = application_documents(self.store, application_id)
            base = next(row for row in base_rows if row["kind"] == "resume")
            base_text = (self.root / application_id / base["files"]["txt"]).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("Led Python operations for 20 teams", base_text)
            prepare(self.store, job["id"], "fixture", self.root, tailor_resume=True)
            generated = application_documents(self.store, application_id)
            tailored = next(row for row in generated
                            if row["kind"] == "resume" and row["version"] == 2)

        # Reopening a new Store models process/service replacement while keeping
        # both PostgreSQL-like records and generated artifact bytes durable.
        self.store.engine.dispose()
        reopened = Store(self.url)
        try:
            with TestClient(create_app(
                reopened, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE"
            )) as client:
                documents_response = client.get(
                    f"/profiles/fixture/applications/{application_id}/documents"
                )
                self.assertEqual(documents_response.status_code, 200, documents_response.text)
                durable = documents_response.json()["documents"]
                durable_base = next(row for row in durable if row["id"] == base["id"])
                durable_tailored = next(row for row in durable if row["id"] == tailored["id"])
                self.assertEqual((durable_base["version"], durable_tailored["version"]), (1, 2))

                compare_url = (
                    f"/profiles/fixture/applications/{application_id}/document-comparison"
                )
                response = client.get(compare_url, params={
                    "base_document_id": durable_base["id"],
                    "tailored_document_id": durable_tailored["id"],
                })
                self.assertEqual(response.status_code, 200, response.text)
                comparison = response.json()

                # Falsifiable oracle: the API must compare these exact persisted
                # revisions, retain the matching fact, omit the irrelevant one,
                # and invent no replacement facts.
                self.assertEqual(comparison["base"]["id"], durable_base["id"])
                self.assertEqual(comparison["base"]["version"], 1)
                self.assertEqual(comparison["tailored"]["id"], durable_tailored["id"])
                self.assertEqual(comparison["tailored"]["version"], 2)
                self.assertEqual(
                    {row["fact_id"] for row in comparison["omitted"]}, {"vmware-work"}
                )
                self.assertEqual(comparison["added"], [])
                self.assertEqual(
                    {row["fact_id"] for row in comparison["tailored"]["facts"]},
                    {"summary", "python", "python-work"},
                )
                self.assertIn("Built Python APIs", comparison["tailored"]["text"])
                self.assertNotIn("Managed VMware hosts", comparison["tailored"]["text"])
                self.assertNotIn("Led Python operations for 20 teams", comparison["tailored"]["text"])

                # Negative control: swapping revision roles must not silently
                # produce an unchanged/successful comparison.
                mismatch = client.get(compare_url, params={
                    "base_document_id": durable_tailored["id"],
                    "tailored_document_id": durable_base["id"],
                })
                self.assertEqual(mismatch.status_code, 409, mismatch.text)
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
