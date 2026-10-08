import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.resume_page_fit import create_resume_page_fit_router
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration import test_document_provenance as fixtures
from tests.integration.test_spine import PROFILE
from fixtures.mock_server import fixture_server


class ResumePageFitApiTests(unittest.TestCase):
    setUp = fixtures.DocumentProvenanceTests.setUp
    tearDown = fixtures.DocumentProvenanceTests.tearDown

    def setUp(self):
        fixtures.DocumentProvenanceTests.setUp(self)
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.calls = {"preview": [], "generate": []}
        self.now = [100.0]
        self.outside_preview_path = None
        self.preview_result = {
            "profile_version": self.store.profile("fixture")["version"],
            "job_fingerprint": "f" * 64,
            "page_fit": {
                "page_target": 1,
                "page_count": 1,
                "attempts": 2,
                "measurement_available": True,
                "target_met": True,
                "reason": None,
                "omitted_facts": [{
                    "fact_id": "optional-project",
                    "reason": "PAGE_TARGET_FIT_LOWEST_RETENTION_RANK",
                }],
            },
        }

        def preview_runner(store, profile_id, job_id, output_dir, target_pdf_pages):
            self.calls["preview"].append((profile_id, job_id, target_pdf_pages))
            preview_root = Path(output_dir) / ".page-fit-previews"
            preview_root.mkdir(parents=True, exist_ok=True)
            preview_path = preview_root / f"preview-{len(self.calls['preview'])}.pdf"
            preview_path.write_bytes(b"%PDF-1.7\nsynthetic")
            path = self.outside_preview_path or preview_path
            return {**self.preview_result, "preview_pdf_path": str(path)}

        def generate_runner(store, profile_id, job_id, output_dir, preview):
            self.calls["generate"].append((profile_id, job_id, preview))
            return {"application_id": "prepared-app", "submit": False}

        app = FastAPI()
        app.include_router(create_resume_page_fit_router(
            self.store, self.root,
            preview_runner=preview_runner,
            generate_runner=generate_runner,
            clock=lambda: self.now[0],
        ))
        self.client = TestClient(app)

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.close()
        fixtures.DocumentProvenanceTests.tearDown(self)

    def url(self, profile="fixture"):
        return f"/profiles/{profile}/jobs/{self.job_id}/resume-page-fit-previews"

    def counts(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
                for table in (m.applications, m.documents, m.operations)
            }

    def test_preview_is_profile_scoped_non_persisting_and_returns_measured_context(self):
        before = self.counts()
        response = self.client.post(self.url(), json={"target_pdf_pages": 1})

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["preview_id"])
        self.assertEqual(result["generation_context"]["page_fit"], self.preview_result["page_fit"])
        self.assertEqual(result["profile_id"], "fixture")
        self.assertEqual(self.calls["preview"], [("fixture", self.job_id, 1)])
        self.assertEqual(self.calls["generate"], [])
        self.assertEqual(self.counts(), before)

    def test_explicit_review_is_required_before_generation(self):
        before = self.counts()
        preview = self.client.post(self.url(), json={"target_pdf_pages": 1}).json()
        response = self.client.post(
            f"{self.url()}/{preview['preview_id']}/generate",
            json={"reviewed": False},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.calls["generate"], [])
        self.assertEqual(self.counts(), before)

    def test_reviewed_generation_is_profile_scoped_and_never_submits(self):
        preview = self.client.post(self.url(), json={"target_pdf_pages": 1}).json()
        response = self.client.post(
            f"{self.url()}/{preview['preview_id']}/generate",
            json={"reviewed": True},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["application_id"], "prepared-app")
        self.assertIs(response.json()["submit"], False)
        self.assertEqual(len(self.calls["generate"]), 1)
        self.assertEqual(self.calls["generate"][0][:2], ("fixture", self.job_id))
        self.assertEqual(self.calls["generate"][0][2]["page_fit"],
                         self.preview_result["page_fit"])
        self.assertFalse((self.root / ".page-fit-previews" / "preview-1.pdf").exists())

    def test_preview_pdf_is_downloadable_only_through_its_scoped_preview(self):
        preview = self.client.post(self.url(), json={"target_pdf_pages": 1}).json()
        response = self.client.get(preview["preview_pdf_url"])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertEqual(response.content, b"%PDF-1.7\nsynthetic")

    def test_unavailable_measurement_is_reported_without_page_guess(self):
        before = self.counts()
        self.preview_result["page_fit"] = {
            "page_target": 1,
            "page_count": 3,
            "attempts": 2,
            "measurement_available": False,
            "target_met": False,
            "reason": "PAGE_MEASUREMENT_UNAVAILABLE",
            "omitted_facts": [],
        }
        response = self.client.post(self.url(), json={"target_pdf_pages": 1})

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["actual_page_count"], 3)
        self.assertEqual(result["measurement_status"], "UNAVAILABLE")
        self.assertFalse(result["unmet_target"])
        self.assertEqual(result["generation_context"]["page_fit"]["page_count"], 3)
        self.assertEqual(result["generation_context"]["page_fit"]["omitted_facts"], [])
        blocked = self.client.post(
            f"{self.url()}/{result['preview_id']}/generate", json={"reviewed": True},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(self.calls["generate"], [])
        self.assertEqual(self.counts(), before)

    def test_measured_over_target_is_reported_as_unmet(self):
        self.preview_result["page_fit"] = {
            "page_target": 1,
            "page_count": 3,
            "attempts": 8,
            "measurement_available": True,
            "target_met": False,
            "reason": "MAX_RENDER_ATTEMPTS_REACHED",
            "omitted_facts": [],
        }
        response = self.client.post(self.url(), json={"target_pdf_pages": 1})

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["measurement_status"], "AVAILABLE")
        self.assertTrue(result["unmet_target"])

    def test_expiry_deletes_only_contained_preview_pdf(self):
        preview = self.client.post(self.url(), json={"target_pdf_pages": 1}).json()
        preview_path = self.root / ".page-fit-previews" / "preview-1.pdf"
        self.assertTrue(preview_path.is_file())
        self.now[0] += 15 * 60 + 1
        self.assertEqual(self.client.get(preview["preview_pdf_url"]).status_code, 404)
        self.assertFalse(preview_path.exists())
        self.assertFalse((self.root / ".page-fit-previews").exists())

    def test_active_preview_cache_is_bounded_and_evicts_oldest_pdf(self):
        before = self.counts()
        responses = [self.client.post(self.url(), json={"target_pdf_pages": 1})
                     for _ in range(101)]
        self.assertTrue(all(response.status_code == 200 for response in responses))
        preview_root = self.root / ".page-fit-previews"
        self.assertFalse((preview_root / "preview-1.pdf").exists())
        self.assertTrue((preview_root / "preview-101.pdf").is_file())
        self.assertLessEqual(len(list(preview_root.glob("*.pdf"))), 100)
        self.assertEqual(self.counts(), before)

    def test_outside_pdf_is_neither_served_nor_deleted_on_expiry(self):
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"private")
        self.outside_preview_path = outside
        response = self.client.post(self.url(), json={"target_pdf_pages": 1})
        preview = response.json()
        self.assertEqual(self.client.get(preview["preview_pdf_url"]).status_code, 404)
        self.now[0] += 15 * 60 + 1
        expired = self.client.get(preview["preview_pdf_url"])
        self.assertEqual(expired.status_code, 404)
        self.assertTrue(outside.is_file())

    def test_foreign_profile_and_stale_profile_previews_cannot_generate(self):
        before = self.counts()
        preview = self.client.post(self.url(), json={"target_pdf_pages": 1}).json()
        foreign = self.client.post(
            f"{self.url('other')}/{preview['preview_id']}/generate",
            json={"reviewed": True},
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

        self.store.save_profile({**PROFILE, "facts": [dict(PROFILE["facts"][0], text="Changed fact")]})
        stale = self.client.post(
            f"{self.url()}/{preview['preview_id']}/generate",
            json={"reviewed": True},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.calls["generate"], [])
        self.assertFalse((self.root / ".page-fit-previews" / "preview-1.pdf").exists())
        self.assertEqual(self.counts(), before)

    def test_expired_preview_is_stale_and_cannot_generate(self):
        before = self.counts()
        preview = self.client.post(self.url(), json={"target_pdf_pages": 1}).json()
        self.now[0] += 15 * 60 + 1
        expired = self.client.post(
            f"{self.url()}/{preview['preview_id']}/generate",
            json={"reviewed": True},
        )

        self.assertEqual(expired.status_code, 404, expired.text)
        self.assertEqual(self.calls["generate"], [])
        self.assertEqual(self.counts(), before)

    def test_page_target_is_bounded_and_strict(self):
        before = self.counts()
        for value in (0, 21, True, 1.5):
            with self.subTest(value=value):
                response = self.client.post(self.url(), json={"target_pdf_pages": value})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.calls["preview"], [])
        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
