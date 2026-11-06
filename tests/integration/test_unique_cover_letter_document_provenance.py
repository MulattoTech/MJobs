"""Cross-job cover-letter facts and immutable upload survive service reopen."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from cli.acceptance_documents import prove_unique_cover_letter_artifacts
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


class UniqueCoverLetterDocumentProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        source = PROFILE["facts"][0]
        self.pending = "PENDING_FACT_CANARY_2924_RED"
        self.profile = dict(PROFILE, facts=[
            dict(source, id="python", kind="skill", text="Python"),
            dict(source, id="sql", kind="skill", text="SQL"),
            dict(source, id="pending", kind="responsibility", text=self.pending,
                 verified=False, user_approved=False),
        ])

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_same_profile_job_letters_are_distinct_truthful_and_recoverable(self):
        expected_titles = {"A": "Python Developer", "B": "Data Engineer"}
        base = b"Synthetic uploaded base resume; retain these exact bytes.\n"
        with fixture_server() as origin, TestClient(create_app(
            self.store, origin, self.root, operation_mode="FIXTURE"
        )) as client:
            client.put("/profiles", json=self.profile).raise_for_status()
            upload = client.post(
                "/profiles/fixture/resumes",
                content=base,
                headers={"content-type": "text/plain", "x-filename": "original.txt"},
            )
            self.assertEqual(upload.status_code, 201, upload.text)
            base_upload = upload.json()
            self.assertEqual(base_upload["sha256"], hashlib.sha256(base).hexdigest())
            discover(self.store, origin, "fixture")
            jobs = {row["requisition"]: row for row in self.store.jobs("fixture")}
            self.assertEqual(set(jobs), set(expected_titles))
            with self.store.engine.begin() as connection:
                connection.execute(update(m.jobs).where(m.jobs.c.id == jobs["B"]["id"]).values(
                    skills=["SQL"], description=(
                        "Responsibilities:\n"
                        "- Design SQL data pipelines for finance teams.\n"
                        "- Maintain database quality checks for analytics systems."
                    ),
                ))

            cases = {}
            for label in ("A", "B"):
                response = client.post("/applications", json={
                    "profile_id": "fixture", "job_id": jobs[label]["id"],
                })
                self.assertEqual(response.status_code, 202, response.text)
                operation = response.json()
                with patch.dict(os.environ, {
                    "MJOBS_DATABASE_URL": self.url,
                    "MJOBS_MOCK_ORIGIN": origin,
                    "MJOBS_OUTPUT_DIR": str(self.root),
                    "MJOBS_OPERATION_MODE": "FIXTURE",
                }):
                    result = execute_operation(operation["operation_id"])
                self.assertEqual(result["status"], "COMPLETED", result)
                documents = client.get(
                    f"/profiles/fixture/applications/{operation['application_id']}/documents"
                )
                self.assertEqual(documents.status_code, 200, documents.text)
                letter = next(row for row in documents.json()["documents"]
                              if row["kind"] == "cover-letter")
                cases[label] = {
                    "application_id": operation["application_id"],
                    "job_id": jobs[label]["id"],
                    "title": expected_titles[label],
                    "company": "Example Systems Inc.",
                    "responsibility": (
                        "Build and support Python and SQL systems" if label == "A"
                        else "Design SQL data pipelines for finance teams"
                    ),
                    "document_id": letter["id"],
                    "version": letter["version"],
                    "fact_ids": ["python", "sql"] if label == "A" else ["sql"],
                }

        # Both generated documents and the original upload are now read only
        # through a newly opened store and public app routes.
        self.store.engine.dispose()
        reopened = Store(self.url)
        try:
            with TestClient(create_app(
                reopened, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE"
            )) as client:
                def get_json(path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, (path, response.text))
                    return response.json()

                def get_bytes(path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, (path, response.text))
                    return response.content

                proof = prove_unique_cover_letter_artifacts(
                    get_json,
                    get_bytes,
                    profile_id="fixture",
                    cases=cases,
                    base_revision_id=base_upload["revision_id"],
                    base_sha256=base_upload["sha256"],
                    base_bytes=base,
                    approved_by_case={
                        "A": ("Python", "SQL"),
                        "B": ("SQL",),
                    },
                    pending_sentinels=(self.pending,),
                )
            self.assertEqual(proof["A"]["profile_id"], proof["B"]["profile_id"])
            self.assertNotEqual(proof["A"]["job_id"], proof["B"]["job_id"])
            self.assertNotEqual(proof["A"]["sha256"]["txt"], proof["B"]["sha256"]["txt"])
            self.assertTrue(proof["base_upload"]["unchanged"])
        finally:
            reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
