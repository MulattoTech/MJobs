"""Measured PDF page-fit generation preserves factual and revision provenance."""
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from pypdf import PdfReader
from sqlalchemy import update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.generate import (
    generate_resume_page_fit_preview,
    prepare,
    preview_resume_page_fit,
)
from services.documents.history import application_documents
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class ResumePageFitIntegrationTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def test_measured_fit_omits_lowest_ranked_bullet_and_preserves_prior_revision(self):
        facts = [
            dict(PROFILE["facts"][0], id="python", kind="skill"),
            dict(PROFILE["facts"][1], id="protected-experience", kind="experience",
                 text="Supported approved Python systems at Example Corp from 2020 to 2024."),
            dict(PROFILE["facts"][0], id="high-relevance", kind="responsibility",
                 text="Automated Python reporting for a verified weekly service workflow."),
            dict(PROFILE["facts"][0], id="low-relevance", kind="responsibility",
                 text="Maintained Python records for a verified regional inventory workflow. " * 280),
        ]
        profile = dict(PROFILE, facts=facts)
        self.store.save_profile(profile)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(skills=["Python"]))

        application_id = prepare(self.store, job["id"], "fixture", self.root)
        first_resume = next(row for row in application_documents(self.store, application_id)
                            if row["kind"] == "resume")
        prior_bytes = (self.root / application_id / first_resume["files"]["pdf"]).read_bytes()
        profile_before_fit = self.store.profile("fixture")

        prepare(self.store, job["id"], "fixture", self.root, tailor_resume=True,
                resume_page_target=1)

        rows = application_documents(self.store, application_id)
        fitted = next(row for row in rows if row["kind"] == "resume" and row["version"] == 2)
        pdf_path = self.root / application_id / fitted["files"]["pdf"]
        actual_pages = len(PdfReader(str(pdf_path)).pages)
        page_fit = fitted["generation_context"]["page_fit"]
        self.assertEqual(page_fit["target_page_count"], 1)
        self.assertEqual(page_fit["actual_page_count"], actual_pages)
        self.assertLessEqual(actual_pages, page_fit["target_page_count"])
        self.assertEqual(page_fit["measurement_status"], "MEASURED")
        self.assertFalse(page_fit["unmet_target"])
        self.assertGreater(page_fit["attempts_used"], 1)
        self.assertEqual(page_fit["omitted_facts"], [{
            "fact_id": "low-relevance", "reason": "PAGE_TARGET_FIT_LOWEST_RETENTION_RANK"
        }])
        self.assertIn("protected-experience", fitted["fact_ids"])
        self.assertIn("high-relevance", fitted["fact_ids"])
        self.assertNotIn("low-relevance", fitted["fact_ids"])
        self.assertEqual(self.store.profile("fixture"), profile_before_fit)
        self.assertEqual((self.root / application_id / first_resume["files"]["pdf"]).read_bytes(),
                         prior_bytes)
        docx_text = "\n".join(paragraph.text for paragraph in Document(
            str(self.root / application_id / fitted["files"]["docx"])).paragraphs)
        txt_text = (self.root / application_id / fitted["files"]["txt"]).read_text(encoding="utf-8").rstrip("\n")
        self.assertEqual(docx_text, txt_text)

        # Identical fit requests are idempotent and do not create another revision.
        prepare(self.store, job["id"], "fixture", self.root, tailor_resume=True,
                resume_page_target=1)
        self.assertEqual(self.store.application(application_id)["document_version"], 2)
        self.assertEqual((self.root / application_id / first_resume["files"]["pdf"]).read_bytes(),
                         prior_bytes)

        prepare(self.store, job["id"], "fixture", self.root, tailor_resume=True,
                resume_page_target=2)
        rows = application_documents(self.store, application_id)
        retargeted = next(row for row in rows if row["kind"] == "resume" and row["version"] == 3)
        self.assertEqual(retargeted["generation_context"]["page_fit"]["target_page_count"], 2)
        self.assertEqual(self.store.application(application_id)["document_version"], 3)

    def test_unmet_target_reports_original_pdf_measurement_without_guessing_or_trimming(self):
        facts = [
            dict(PROFILE["facts"][0], id="python", kind="skill"),
            dict(PROFILE["facts"][0], id="long-protected", kind="experience",
                 text="Supported Python systems. " * 900),
            *[
                dict(PROFILE["facts"][0], id=f"optional-{index}", kind="responsibility",
                     text="Maintained Python service records for a verified operational workflow. " * 120)
                for index in range(9)
            ],
        ]
        self.store.save_profile(dict(PROFILE, facts=facts))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(skills=["Python"]))

        application_id = prepare(self.store, job["id"], "fixture", self.root, tailor_resume=True,
                                 resume_page_target=1)

        resume = next(row for row in application_documents(self.store, application_id)
                      if row["kind"] == "resume")
        pages = len(PdfReader(str(self.root / application_id / resume["files"]["pdf"])).pages)
        result = resume["generation_context"]["page_fit"]
        self.assertEqual(result["actual_page_count"], pages)
        self.assertGreater(pages, 1)
        self.assertEqual(result["measurement_status"], "MEASURED")
        self.assertTrue(result["unmet_target"])
        self.assertEqual(result["omitted_facts"], [])
        self.assertIn("long-protected", resume["fact_ids"])
        self.assertEqual(result["attempts_used"], 8)
        self.assertEqual(result["max_attempts"], 8)
        self.assertTrue({f"optional-{index}" for index in range(9)}.issubset(resume["fact_ids"]))

    def test_unavailable_measurement_records_no_page_count_or_omission(self):
        facts = [
            dict(PROFILE["facts"][0], id="python", kind="skill"),
            dict(PROFILE["facts"][0], id="approved-bullet", kind="responsibility",
                 text="Maintained approved Python service records."),
        ]
        self.store.save_profile(dict(PROFILE, facts=facts))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(skills=["Python"]))

        with patch("services.documents.generate.PdfReader", side_effect=OSError("measurement unavailable")):
            application_id = prepare(self.store, job["id"], "fixture", self.root,
                                     tailor_resume=True, resume_page_target=1)

        resume = next(row for row in application_documents(self.store, application_id)
                      if row["kind"] == "resume")
        result = resume["generation_context"]["page_fit"]
        self.assertEqual(result["measurement_status"], "UNAVAILABLE")
        self.assertIsNone(result["actual_page_count"])
        self.assertFalse(result["unmet_target"])
        self.assertEqual(result["attempts_used"], 1)
        self.assertEqual(result["omitted_facts"], [])
        self.assertIn("approved-bullet", resume["fact_ids"])

    def test_preview_is_transient_and_only_explicit_generation_persists_a_revision(self):
        facts = [
            dict(PROFILE["facts"][0], id="python", kind="skill"),
            dict(PROFILE["facts"][0], id="approved-bullet", kind="responsibility",
                 text="Maintained approved Python service records."),
        ]
        self.store.save_profile(dict(PROFILE, facts=facts))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(skills=["Python"]))
        application_count = self.store.count("applications")
        document_count = self.store.count("documents")

        preview = preview_resume_page_fit(self.store, "fixture", job["id"], self.root, 1)

        self.assertEqual(self.store.count("applications"), application_count)
        self.assertEqual(self.store.count("documents"), document_count)
        preview_path = Path(preview["preview_pdf_path"])
        self.assertTrue(preview_path.is_file())
        self.assertEqual(preview_path.parent, self.root.resolve() / ".page-fit-previews")
        self.assertEqual(len(PdfReader(str(preview_path)).pages), preview["page_fit"]["page_count"])

        generated = generate_resume_page_fit_preview(
            self.store, "fixture", job["id"], self.root, preview)

        self.assertEqual(self.store.count("applications"), application_count + 1)
        self.assertEqual(self.store.count("documents"), document_count + 2)
        self.assertFalse(preview_path.exists())
        resume = next(row for row in application_documents(
            self.store, generated["application_id"]) if row["kind"] == "resume")
        self.assertEqual(resume["generation_context"]["page_fit"]["actual_page_count"],
                         preview["page_fit"]["page_count"])

    def test_preview_generation_rejects_a_changed_profile_without_creating_documents(self):
        self.store.save_profile(dict(PROFILE, facts=[
            dict(PROFILE["facts"][0], id="python", kind="skill"),
            dict(PROFILE["facts"][0], id="approved-bullet", kind="responsibility",
                 text="Maintained approved Python service records."),
        ]))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(skills=["Python"]))
        preview = preview_resume_page_fit(self.store, "fixture", job["id"], self.root, 1)
        preview_path = Path(preview["preview_pdf_path"])
        before_applications = self.store.count("applications")
        before_documents = self.store.count("documents")
        self.store.save_profile(dict(PROFILE, name="Changed after preview", facts=[
            dict(PROFILE["facts"][0], id="python", kind="skill"),
            dict(PROFILE["facts"][0], id="approved-bullet", kind="responsibility",
                 text="Maintained approved Python service records."),
        ]))

        with self.assertRaises(ValueError):
            generate_resume_page_fit_preview(
                self.store, "fixture", job["id"], self.root, preview)

        self.assertEqual(self.store.count("applications"), before_applications)
        self.assertEqual(self.store.count("documents"), before_documents)
        self.assertFalse(preview_path.exists())


if __name__ == "__main__":
    unittest.main()
