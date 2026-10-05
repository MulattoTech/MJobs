"""Durable style/length revisions and validation-backed preview/downloads."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from docx import Document as Docx
from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.documents.render import render_pdf
from packages.persistence import models as m
from tests.integration.test_spine import PROFILE


class CoverLetterWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'trial.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(copy.deepcopy(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_style_and_length_create_reviewable_immutable_versions_and_bad_bytes_block_download(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs() if row["requisition"] == "B")
            application_id = prepare(self.store, job["id"], "fixture", self.root)
            old_documents = application_documents(self.store, application_id, self.root)
            old_letter = next(row for row in old_documents if row["kind"] == "cover-letter")
            old_bytes = {fmt: (self.root / application_id / path).read_bytes()
                         for fmt, path in old_letter["files"].items()}
            self.assertEqual(old_letter["generation_context"]["style"], "professional")
            self.assertIn("Application for", old_letter["preview_text"])
            self.assertIn("The posting explicitly requires: Python; SQL.", old_letter["preview_text"])
            self.assertEqual(old_letter["fact_ids"], ["python", "sql"])

            same = prepare(self.store, job["id"], "fixture", self.root,
                           cover_letter_style="warm", cover_letter_length="detailed")
            self.assertEqual(same, application_id)
            history = application_documents(self.store, application_id, self.root)
            letters = [row for row in history if row["kind"] == "cover-letter"]
            self.assertEqual({row["version"] for row in letters}, {1, 2})
            newest = next(row for row in letters if row["version"] == 2)
            self.assertEqual(newest["generation_context"]["style"], "warm")
            self.assertEqual(newest["generation_context"]["length"], "detailed")
            self.assertIn("I would be glad to discuss", newest["preview_text"])
            self.assertNotEqual(newest["preview_text"], old_letter["preview_text"])
            self.assertIsNone(newest["preview_error"])
            for fmt, payload in old_bytes.items():
                self.assertEqual((self.root / application_id / old_letter["files"][fmt]).read_bytes(), payload)
                self.assertEqual(hashlib.sha256(payload).hexdigest(), old_letter["sha256"][fmt])

            current = next(row for row in letters if row["version"] == 2)
            text_path = self.root / application_id / current["files"]["txt"]
            text_path.write_text(current["preview_text"] + "\nInvented employer motivation.\n", encoding="utf-8")
            invalidated = next(row for row in application_documents(self.store, application_id, self.root)
                               if row["id"] == current["id"])
            self.assertIsNone(invalidated["preview_text"])
            self.assertIn("validation failed", invalidated["preview_error"].casefold())

            app = create_app(self.store, origin, self.root, operation_mode="FIXTURE")
            with TestClient(app) as client:
                response = client.get(f"/documents/{current['id']}/txt")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.store.application(application_id)["document_version"], 2)

    def test_legacy_generic_cover_letter_remains_previewable_and_downloadable(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs() if row["requisition"] == "B")
            application_id = prepare(self.store, job["id"], "fixture", self.root)
            row = next(row for row in application_documents(self.store, application_id, self.root)
                       if row["kind"] == "cover-letter")

            # Recreate the stored shape/content contract from before job-specific letters.
            profile = self.store.profile("fixture", row["profile_version"])
            fact_ids = row["fact_ids"]
            fact_text = {fact["fact_id"]: fact["text"] for fact in profile["facts"]}
            paragraphs = [profile["name"], f"Application for {job['title']}",
                          *(fact_text[identifier] for identifier in fact_ids)]
            files = {fmt: self.root / application_id / relative
                     for fmt, relative in row["files"].items()}
            word = Docx()
            for paragraph in paragraphs:
                word.add_paragraph(paragraph)
            word.save(files["docx"])
            render_pdf(files["pdf"], paragraphs)
            files["txt"].write_text("\n".join(paragraphs) + "\n", encoding="utf-8")
            hashes = {fmt: hashlib.sha256(path.read_bytes()).hexdigest()
                      for fmt, path in files.items()}
            with self.store.engine.begin() as connection:
                connection.execute(update(m.documents).where(m.documents.c.id == row["id"]).values(
                    template="verified-facts-v1", generation_model="deterministic-fact-copy-v1",
                    generation_context=None, sha256=hashes))

            app = create_app(self.store, origin, self.root, operation_mode="FIXTURE")
            with TestClient(app) as client:
                history = client.get(f"/applications/{application_id}/documents")
                response = client.get(f"/documents/{row['id']}/txt")
            self.assertEqual(history.status_code, 200)
            legacy = next(item for item in history.json()["documents"] if item["id"] == row["id"])
            self.assertEqual(legacy["preview_text"], "\n".join(paragraphs))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, files["txt"].read_bytes())


if __name__ == "__main__":
    unittest.main()
