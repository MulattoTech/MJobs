"""Fail-closed document generation and explicit recovery flow."""
from pathlib import Path
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

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
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.create_database())
        self.addCleanup(self.store.engine.dispose)
        self.migrate_database()

    def create_database(self):
        return f"sqlite:///{(self.root / 'test.db').as_posix()}"

    def migrate_database(self):
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

    def test_renderer_exception_is_sanitized_and_same_operation_can_be_retried(self):
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

            renderer_failed = {"value": False}

            def fail_renderer(*args, **kwargs):
                renderer_failed["value"] = True
                raise OSError("private renderer path and profile contents")

            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                            MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root),
                            MJOBS_OPERATION_MODE="FIXTURE"), \
                    patch("services.documents.generate.render_pdf", side_effect=fail_renderer):
                failed = execute_operation(operation_id)

            self.assertTrue(renderer_failed["value"], "fault injection must reach the PDF renderer")
            self.assertEqual(failed["status"], "DEAD_LETTERED")
            failed_operation = client.get(f"/operations/{operation_id}").json()
            self.assertEqual(failed_operation["error"], "DOCUMENT_GENERATION_FAILED")
            self.assertNotIn("private renderer", str(failed_operation))
            dead_letters = client.get("/operations/dead-letter-queue?profile_id=fixture").json()
            self.assertEqual(dead_letters["count"], 1)
            self.assertTrue(dead_letters["operations"][0]["retryable"])
            self.assertEqual(self.store.count("applications"), 0)
            self.assertEqual(self.store.count("documents"), 0)
            for extension in ("docx", "pdf", "txt"):
                self.assertEqual(list(self.root.rglob(f"*.{extension}")), [])
            self.assertEqual(list(self.root.rglob("*.tmp")), [])
            self.assertNotEqual(
                client.get(f"/profiles/fixture/applications/{application_id}").status_code,
                200,
            )

            retry = client.post(f"/operations/{operation_id}/retry")
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertEqual(retry.json(), {"operation_id": operation_id, "state": "QUEUED"})
            with patch.dict("os.environ", MJOBS_DATABASE_URL=self.store.url,
                            MJOBS_MOCK_ORIGIN=origin, MJOBS_OUTPUT_DIR=str(self.root),
                            MJOBS_OPERATION_MODE="FIXTURE"):
                recovered = execute_operation(operation_id)

            self.assertEqual(recovered["status"], "COMPLETED")
            self.assertEqual(client.get(f"/operations/{operation_id}").json()["state"], "COMPLETED")
            self.assertEqual(self.store.count("applications"), 1)
            self.assertEqual(self.store.count("documents"), 2)
            self.assertEqual(
                client.get(f"/profiles/fixture/applications/{application_id}").json()["state"],
                "PREPARED",
            )


@unittest.skipUnless(os.environ.get("MJOBS_APPLICATION_CRASH_POSTGRES_URL"),
                     "Requires a disposable loopback mjobs_test_* PostgreSQL database")
class PostgreSQLDocumentGenerationFailureFlowTests(DocumentGenerationFailureFlowTests):
    """Run the same real worker rollback/retry contract on the released schema."""

    def create_database(self):
        url = make_url(os.environ["MJOBS_APPLICATION_CRASH_POSTGRES_URL"])
        if (not url.drivername.startswith("postgresql")
                or url.host not in ("localhost", "127.0.0.1", "::1")
                or not url.database or not url.database.startswith("mjobs_test_")
                or set(url.query) - {"sslmode", "connect_timeout", "application_name"}):
            raise RuntimeError("A disposable loopback mjobs_test_* PostgreSQL database is required")
        self.schema = "document_failure_" + uuid4().hex
        self.admin = create_engine(url, connect_args={"connect_timeout": 10})
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.addCleanup(self.drop_schema)
        return url.update_query_dict({
            "options": f"-csearch_path={self.schema} -clock_timeout=5000 -cstatement_timeout=30000",
        }).render_as_string(hide_password=False)

    def migrate_database(self):
        with patch.dict(os.environ, MJOBS_DATABASE_URL=self.store.url):
            command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))


if __name__ == "__main__":
    unittest.main()
