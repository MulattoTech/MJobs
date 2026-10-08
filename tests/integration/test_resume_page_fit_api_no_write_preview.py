"""API contract for transient, explicitly reviewed resume page-fit previews."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.resume_page_fit import create_resume_page_fit_router
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from services.discovery.pipeline import discover
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE


class ResumePageFitApiNoWritePreviewTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def setUp(self):
        fixtures.DocumentProvenanceTests.setUp(self)
        self.store.save_profile(dict(PROFILE, id="fixture"))
        self.store.save_profile(dict(PROFILE, id="other"))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.calls = {"preview": 0, "generate": 0}
        self.now = [100.0]
        self.preview_result = {
            "profile_version": self.store.profile("fixture")["version"],
            "job_fingerprint": "f" * 64,
            "preview_pdf_path": str(self.root / "transient-preview.pdf"),
            "page_fit": {
                "page_target": 1,
                "page_count": 1,
                "attempts": 2,
                "measurement_available": True,
                "target_met": True,
                "reason": None,
                "omitted_facts": [],
            },
        }

        def preview_runner(store, profile_id, job_id, output_dir, target_pdf_pages):
            self.calls["preview"] += 1
            return dict(self.preview_result)

        def generate_runner(store, profile_id, job_id, output_dir, preview):
            self.calls["generate"] += 1
            return {"application_id": "prepared-app"}

        app = FastAPI()
        app.include_router(create_resume_page_fit_router(
            self.store,
            self.root,
            preview_runner=preview_runner,
            generate_runner=generate_runner,
            clock=lambda: self.now[0],
        ))
        self.client = TestClient(app)

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.close()
        fixtures.DocumentProvenanceTests.tearDown(self)

    def preview_url(self, profile_id="fixture"):
        return f"/profiles/{profile_id}/jobs/{self.job_id}/resume-page-fit-previews"

    def counts(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
                for table in (m.applications, m.documents, m.operations)
            }

    def create_preview(self):
        response = self.client.post(self.preview_url(), json={"target_pdf_pages": 1})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_preview_reports_measurement_without_creating_application_or_document_rows(self):
        before = self.counts()

        preview = self.create_preview()

        self.assertTrue(preview["preview_id"])
        self.assertEqual(preview["actual_page_count"], 1)
        self.assertEqual(preview["generation_context"]["page_fit"],
                         self.preview_result["page_fit"])
        self.assertEqual(self.calls, {"preview": 1, "generate": 0})
        self.assertEqual(self.counts(), before)

    def test_generation_requires_a_strict_explicit_review_true(self):
        preview = self.create_preview()
        url = f"{self.preview_url()}/{preview['preview_id']}/generate"
        before = self.counts()

        for body in ({}, {"reviewed": False}, {"reviewed": 1}, {"reviewed": "true"}):
            with self.subTest(body=body):
                response = self.client.post(url, json=body)
                self.assertIn(response.status_code, (409, 422), response.text)
                self.assertEqual(self.calls["generate"], 0)
                self.assertEqual(self.counts(), before)

        accepted = self.client.post(url, json={"reviewed": True})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["application_id"], "prepared-app")
        self.assertIs(accepted.json()["submit"], False)
        self.assertEqual(self.calls["generate"], 1)

    def test_profile_scope_and_expired_previews_are_rejected_before_generation(self):
        before = self.counts()
        preview = self.create_preview()

        foreign = self.client.post(
            f"{self.preview_url('other')}/{preview['preview_id']}/generate",
            json={"reviewed": True},
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

        self.now[0] += 15 * 60 + 1
        expired = self.client.post(
            f"{self.preview_url()}/{preview['preview_id']}/generate",
            json={"reviewed": True},
        )
        self.assertEqual(expired.status_code, 404, expired.text)
        self.assertEqual(self.calls["generate"], 0)
        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
