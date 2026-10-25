"""Connected synthetic acceptance for MJ-REQ-292 criterion 35."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class UniqueCoverLetterCriterion35Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        source = PROFILE["facts"][0]
        facts = [
            dict(source, id="python", kind="skill", text="Python"),
            dict(source, id="sql", kind="skill", text="SQL"),
            dict(source, id="unapproved-python", kind="responsibility",
                 text="Led Python and SQL operations for 20 teams.", verified=False,
                 user_approved=False),
        ]
        self.profile = dict(PROFILE, facts=facts)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_distinct_mock_jobs_get_truthful_job_bound_letters_after_reopen(self):
        expected_titles = {"A": "Python Developer", "B": "Data Engineer"}
        generated = {}

        with fixture_server() as origin, TestClient(create_app(
            self.store, origin, self.root, operation_mode="FIXTURE"
        )) as client:
            client.put("/profiles", json=self.profile).raise_for_status()
            discover(self.store, origin, "fixture")
            jobs = {row["requisition"]: row for row in self.store.jobs("fixture")}
            self.assertEqual(set(jobs), set(expected_titles))
            self.assertEqual({row["title"] for row in jobs.values()}, set(expected_titles.values()))
            with self.store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == jobs["B"]["id"]).values(
                    skills=["SQL"],
                    description=("Responsibilities:\n"
                                 "- Design SQL data pipelines for finance teams.\n"
                                 "- Maintain database quality checks for analytics systems."),
                ))

            for requisition, title in expected_titles.items():
                queued = client.post("/applications", json={
                    "profile_id": "fixture",
                    "job_id": jobs[requisition]["id"],
                })
                self.assertEqual(queued.status_code, 202, queued.text)
                operation = queued.json()
                self.assertEqual(operation["state"], "QUEUED")
                with patch.dict(os.environ, {
                    "MJOBS_DATABASE_URL": self.url,
                    "MJOBS_MOCK_ORIGIN": origin,
                    "MJOBS_OUTPUT_DIR": str(self.root),
                    "MJOBS_OPERATION_MODE": "FIXTURE",
                }):
                    result = execute_operation(operation["operation_id"])
                self.assertEqual(result["status"], "COMPLETED", result)

                application_id = operation["application_id"]
                documents = client.get(
                    f"/profiles/fixture/applications/{application_id}/documents"
                )
                self.assertEqual(documents.status_code, 200, documents.text)
                letter = next(row for row in documents.json()["documents"]
                              if row["kind"] == "cover-letter")
                content = client.get(f"/profiles/fixture/documents/{letter['id']}/txt")
                self.assertEqual(content.status_code, 200, content.text)
                generated[requisition] = {
                    "application_id": application_id,
                    "letter_id": letter["id"],
                    "version": letter["version"],
                    "sha256": letter["sha256"]["txt"],
                    "text": content.text,
                    "title": title,
                }

        # Reopen the persisted store and read both independently prepared
        # letters back through the profile-scoped public API.
        self.store.engine.dispose()
        reopened = Store(self.url)
        try:
            with TestClient(create_app(
                reopened, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE"
            )) as client:
                readback = {}
                for requisition, expected in generated.items():
                    documents = client.get(
                        f"/profiles/fixture/applications/{expected['application_id']}/documents"
                    )
                    self.assertEqual(documents.status_code, 200, documents.text)
                    letter = next(row for row in documents.json()["documents"]
                                  if row["kind"] == "cover-letter")
                    self.assertEqual(letter["id"], expected["letter_id"])
                    self.assertEqual(letter["version"], expected["version"])
                    content = client.get(f"/profiles/fixture/documents/{letter['id']}/txt")
                    self.assertEqual(content.status_code, 200, content.text)
                    self.assertEqual(letter["sha256"]["txt"], expected["sha256"])
                    self.assertEqual(content.text, expected["text"])
                    self.assertIn(f"Application for {expected['title']} at Example Systems Inc.", content.text)
                    other_title = expected_titles["B" if requisition == "A" else "A"]
                    self.assertNotIn(f"Application for {other_title} at Example Systems Inc.", content.text)
                    expected_facts = ("Python; SQL" if requisition == "A" else "SQL")
                    self.assertIn(
                        f"Approved career facts relevant to these details: {expected_facts}.",
                        content.text,
                    )
                    if requisition == "A":
                        self.assertIn("Build and support Python and SQL systems", content.text)
                    else:
                        self.assertIn("Design SQL data pipelines for finance teams", content.text)
                        self.assertNotIn("Build and support Python and SQL systems", content.text)
                        self.assertNotIn("Approved career facts relevant to these details: Python", content.text)
                    self.assertNotIn("Led Python and SQL operations for 20 teams", content.text)
                    readback[requisition] = content.text

                # Falsifiable uniqueness oracle: same profile and employer but
                # different target roles must not receive a byte-identical,
                # generic cover letter.
                self.assertNotEqual(readback["A"], readback["B"])
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
