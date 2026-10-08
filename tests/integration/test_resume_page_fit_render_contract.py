"""PDF render failures preserve all approved resume facts and output provenance."""
import hashlib
import unittest
from unittest.mock import patch

from pypdf import PdfReader
from sqlalchemy import update

from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class ResumePageFitRenderContractTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def test_second_measurement_failure_keeps_all_facts_and_output_provenance(self):
        source_facts = [
            dict(PROFILE["facts"][0], id="python", kind="skill"),
            dict(PROFILE["facts"][0], id="long-protected", kind="experience",
                 text="Protected experience marker. " * 900),
            dict(PROFILE["facts"][0], id="optional-first", kind="responsibility",
                 text="First approved responsibility marker. " * 260),
            dict(PROFILE["facts"][0], id="optional-second", kind="project",
                 text="Second approved project marker. " * 260),
        ]
        self.store.save_profile(dict(PROFILE, facts=source_facts))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == job["id"]).values(skills=["Python"]))

        measure_calls = 0

        def fail_after_one_real_measurement(path):
            nonlocal measure_calls
            measure_calls += 1
            if measure_calls == 1:
                return PdfReader(path)
            raise OSError("synthetic second PDF measurement failure")

        with patch("services.documents.generate.PdfReader",
                   side_effect=fail_after_one_real_measurement):
            application_id = prepare(self.store, job["id"], "fixture", self.root,
                                     tailor_resume=True, resume_page_target=1)

        resume = next(row for row in application_documents(self.store, application_id)
                      if row["kind"] == "resume")
        output_paths = {
            kind: self.root / application_id / relative_path
            for kind, relative_path in resume["files"].items()
        }
        persisted_pages = len(PdfReader(str(output_paths["pdf"])).pages)
        page_fit = resume["generation_context"]["page_fit"]

        self.assertEqual(measure_calls, 2)
        self.assertGreater(persisted_pages, 1)
        self.assertEqual(page_fit["measurement_status"], "UNAVAILABLE")
        self.assertEqual(page_fit["actual_page_count"], persisted_pages)
        self.assertFalse(page_fit["unmet_target"])
        self.assertEqual(page_fit["attempts_used"], 2)
        self.assertEqual(page_fit["omitted_facts"], [])
        self.assertEqual(set(resume["fact_ids"]), {fact["id"] for fact in source_facts})

        for kind, path in output_paths.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), resume["sha256"][kind])

        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(
            str(output_paths["pdf"])).pages)
        self.assertIn("Protected experience marker.", pdf_text)
        self.assertIn("First approved responsibility marker.", pdf_text)
        self.assertIn("Second approved project marker.", pdf_text)


if __name__ == "__main__":
    unittest.main()
