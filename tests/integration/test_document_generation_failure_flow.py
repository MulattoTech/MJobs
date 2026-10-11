"""Fail-closed document generation and explicit recovery flow."""
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE
from fixtures.mock_server import fixture_server
from workers.tasks import execute_operation


class DocumentGenerationFailureFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-document-failure-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_corrupt_generation_is_dead_lettered_hidden_then_explicitly_retried(self):
        with fixture_server() as origin, TestClient(create_app(
                self.store, origin, self.root, operation_mode="FIXTURE")) as client:
            client.put("/profiles", json=PROFILE).raise_for_status()
            discover(self.store, origin, "fixture")
            job = self.store.jobs("fixture")[0]
            queued_response = client.post("/applications", json={
                "profile_id": "fixture",
                "job_id": job["id"],
            })
            self.assertEqual(queued_response.status_code, 202, queued_response.text)
            queued = queued_response.json()
            application_id = queued["application_id"]
            operation_id = queued["operation_id"]

            from services.documents import generate
            original_atomic_generate = generate.atomic_generate
            corrupted = {"done": False}

            def corrupt_first_resume_docx(path, writer):
                original_atomic_generate(path, writer)
                if Path(path).name == "resume.docx" and not corrupted["done"]:
                    Path(path).write_bytes(b"synthetic truncated DOCX")
                    corrupted["done"] = True

            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                            MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root),
                            MJOBS_OPERATION_MODE="FIXTURE"), \
                    patch("services.documents.generate.atomic_generate",
                          side_effect=corrupt_first_resume_docx):
                failed = execute_operation(operation_id)

            self.assertTrue(corrupted["done"], "fault injection must reach generated output")
            self.assertEqual(failed["status"], "DEAD_LETTERED")
            failed_operation = client.get(f"/operations/{operation_id}").json()
            self.assertEqual(failed_operation["state"], "DEAD_LETTERED")
            self.assertEqual(failed_operation["error"], "DOCUMENT_GENERATION_FAILED")
            dead_letters = client.get("/operations/dead-letter-queue?profile_id=fixture").json()
            self.assertEqual(dead_letters["count"], 1)
            self.assertEqual(dead_letters["operations"][0]["id"], operation_id)
            self.assertTrue(dead_letters["operations"][0]["retryable"])

            # Generation and application metadata commit together. A corrupt
            # intermediate file must not be returned by document readback or
            # leave any published artifact behind.
            self.assertEqual(self.store.count("applications"), 0)
            self.assertEqual(self.store.count("documents"), 0)
            self.assertEqual(list(self.root.rglob("*.docx")), [])
            self.assertEqual(list(self.root.rglob("*.pdf")), [])
            self.assertEqual(list(self.root.rglob("*.txt")), [])
            self.assertNotEqual(
                client.get(f"/profiles/fixture/applications/{application_id}").status_code,
                200,
                "failed generation must never appear as a prepared application",
            )

            # Recovery is an explicit user action on the terminal operation;
            # after the transient corruption is removed, the same request can
            # safely publish a complete validated revision.
            retry = client.post(f"/operations/{operation_id}/retry")
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertEqual(retry.json(), {"operation_id": operation_id, "state": "QUEUED"})
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                            MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root),
                            MJOBS_OPERATION_MODE="FIXTURE"):
                recovered = execute_operation(operation_id)

            self.assertEqual(recovered["status"], "COMPLETED")
            self.assertEqual(client.get(f"/operations/{operation_id}").json()["state"], "COMPLETED")
            application = client.get(
                f"/profiles/fixture/applications/{application_id}"
            )
            self.assertEqual(application.status_code, 200, application.text)
            self.assertEqual(application.json()["state"], "PREPARED")
            documents = client.get(
                f"/profiles/fixture/applications/{application_id}/documents"
            )
            self.assertEqual(documents.status_code, 200, documents.text)
            self.assertEqual({item["kind"] for item in documents.json()["documents"]},
                             {"resume", "cover-letter"})
            self.assertEqual(self.store.count("documents"), 2)
            for document in documents.json()["documents"]:
                for relative_path in document["files"].values():
                    artifact = self.root / application_id / relative_path
                    self.assertGreater(artifact.stat().st_size, 0)
                    self.assertEqual(
                        hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        document["sha256"][artifact.suffix[1:]],
                    )


if __name__ == "__main__":
    unittest.main()
