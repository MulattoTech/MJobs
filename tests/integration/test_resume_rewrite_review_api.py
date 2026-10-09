"""Read-only profile/job preview for user-proposed resume fact wording."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from apps.api.main import create_app
from apps.api.resume_rewrite_review import create_resume_rewrite_review_router
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.documents.tailor import target_fingerprint
from tests.integration.test_spine import PROFILE


class ResumeRewriteReviewApiTests(unittest.TestCase):
    SOURCE = (
        "Supported 40 clinics in 2020-2022 and earned Cisco CCNA certification."
    )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-resume-rewrite-review-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'rewrite-review.sqlite').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile({
            **PROFILE,
            "facts": [
                self.fact("experience", self.SOURCE, "experience"),
                self.fact("pending", "Supported a pending system migration.", "experience",
                          user_approved=False),
                self.fact("education", "Completed systems coursework.", "education"),
                self.fact("other", "Administered approved Linux services.", "responsibility"),
            ],
        })
        self.store.save_profile({
            **PROFILE,
            "id": "other",
            "name": "Other Profile",
            "facts": [self.fact("other-experience", "Other profile source.", "experience")],
        })
        self.job_id = "resume-rewrite-review-job"
        self._seed_job()
        app = FastAPI()
        app.include_router(create_resume_rewrite_review_router(self.store))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    @staticmethod
    def fact(fact_id, text, kind, *, user_approved=True):
        return {
            "id": fact_id,
            "text": text,
            "kind": kind,
            "source": "synthetic-api-test",
            "verified": True,
            "user_approved": user_approved,
            "confidence": 95,
        }

    def _seed_job(self):
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "resume-rewrite-review-company", "name": "Synthetic Systems",
                "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": self.job_id, "company_id": "resume-rewrite-review-company",
                "requisition": "resume-rewrite-review-job",
                "title": "Systems Engineer", "description": "Synthetic resume rewrite target.",
                "skills": ["Linux"], "location": "Remote", "remote_type": "REMOTE",
                "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "resume-rewrite-review-route", "job_id": self.job_id,
                "company_id": "resume-rewrite-review-company", "provider": "fixture",
                "tenant": "resume-rewrite-review", "requisition": "resume-rewrite-review-job",
                "url": "https://jobs.example.test/resume-rewrite-review",
            })

    def url(self, profile_id="fixture", job_id=None):
        return (f"/profiles/{profile_id}/jobs/{job_id or self.job_id}"
                "/resume-rewrite-review-preview")

    def request(self, *, profile_id="fixture", job_id=None, selected=None, rewrites=None,
                expected_profile_version=None, expected_job_fingerprint=None):
        profile = self.store.profile(profile_id)
        job = self.store.job(job_id or self.job_id, profile_id)
        return {
            "expected_profile_version": expected_profile_version or profile["version"],
            "expected_job_fingerprint": expected_job_fingerprint or target_fingerprint(job),
            "selected_fact_ids": ["experience"] if selected is None else selected,
            "rewrites": [{"fact_id": "experience", "text":
                          "Supported 40 clinics through Cisco CCNA-certified service work in 2020-2022."}
                         ] if rewrites is None else rewrites,
        }

    def counts(self):
        with self.store.engine.connect() as connection:
            return {
                table.name: connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
                for table in (m.facts, m.applications, m.documents, m.operations, m.audit)
            }

    def test_application_mount_exposes_resume_rewrite_review_preview(self):
        app = create_app(
            self.store,
            "http://127.0.0.1:1",
            self.root / "documents",
            "redis://127.0.0.1:1/0",
            operation_mode="FIXTURE",
        )
        with TestClient(app) as client:
            response = client.post(self.url(), json=self.request())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job_id"], self.job_id)
        self.assertTrue(response.json()["read_only"])

    def test_preview_returns_exact_source_hash_token_audit_and_proposed_text_without_writes(self):
        before_counts = self.counts()
        before_facts = self.store.profile("fixture")["facts"]
        response = self.client.post(self.url(), json=self.request())

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "fixture")
        self.assertEqual(body["selected_fact_ids"], ["experience"])
        self.assertEqual(body["profile_version"], self.store.profile("fixture")["version"])
        self.assertEqual(body["job_id"], self.job_id)
        self.assertEqual(body["job_fingerprint"], target_fingerprint(
            self.store.job(self.job_id, "fixture")))
        self.assertTrue(body["read_only"])
        self.assertTrue(body["review_required"])
        self.assertEqual(body["semantic_equivalence"], "USER_REVIEW_REQUIRED")
        self.assertEqual(len(body["rewrites"]), 1)
        preview = body["rewrites"][0]
        self.assertEqual(preview["fact_id"], "experience")
        self.assertEqual(preview["fact_kind"], "experience")
        self.assertEqual(preview["source_text"], self.SOURCE)
        self.assertEqual(preview["source_sha256"], hashlib.sha256(
            self.SOURCE.encode("utf-8")).hexdigest())
        self.assertEqual(preview["proposed_text"], self.request()["rewrites"][0]["text"])
        audit = preview["protected_token_audit"]
        self.assertTrue(audit["required"])
        self.assertTrue(audit["retained"])
        self.assertEqual(audit["missing"], [])
        self.assertTrue(any(item["type"] == "DATE" for item in audit["required"]))
        self.assertTrue(any(item["type"] == "NUMBER" for item in audit["required"]))
        self.assertTrue(any(item["type"] == "NAMED_CREDENTIAL" for item in audit["required"]))
        self.assertEqual(self.counts(), before_counts)
        self.assertEqual(self.store.profile("fixture")["facts"], before_facts)

    def test_missing_protected_tokens_are_returned_for_correction_without_persistence(self):
        before = self.counts()
        response = self.client.post(self.url(), json=self.request(rewrites=[{
            "fact_id": "experience", "text": "Provided user support for clinic systems."
        }]))

        self.assertEqual(response.status_code, 200, response.text)
        audit = response.json()["rewrites"][0]["protected_token_audit"]
        self.assertTrue(audit["missing"])
        self.assertFalse(audit["retained"])
        self.assertEqual(self.counts(), before)

    def test_profile_job_and_source_snapshot_mismatches_fail_without_writes(self):
        before = self.counts()
        request = self.request()
        stale_profile = self.client.post(self.url(), json={
            **request, "expected_profile_version": "0" * 64,
        })
        stale_job = self.client.post(self.url(), json={
            **request, "expected_job_fingerprint": "0" * 64,
        })
        foreign = self.client.post(self.url("other"), json=self.request(
            profile_id="other", selected=["experience"], rewrites=[{
                "fact_id": "experience", "text": "Updated foreign fact reference."
            }]))

        self.assertEqual(stale_profile.status_code, 409, stale_profile.text)
        self.assertEqual(stale_profile.json()["detail"]["code"],
                         "RESUME_REWRITE_PROFILE_STALE")
        self.assertEqual(stale_job.status_code, 409, stale_job.text)
        self.assertEqual(stale_job.json()["detail"]["code"],
                         "RESUME_REWRITE_JOB_STALE")
        self.assertEqual(foreign.status_code, 422, foreign.text)
        self.assertEqual(foreign.json()["detail"]["code"],
                         "RESUME_REWRITE_FACT_INVALID")
        self.assertEqual(self.counts(), before)

    def test_unapproved_unselected_unsupported_and_duplicate_facts_fail_closed(self):
        before = self.counts()
        cases = [
            (["pending"], [{"fact_id": "pending", "text": "Updated pending claim."}],
             "RESUME_REWRITE_FACT_INVALID"),
            (["education"], [{"fact_id": "education", "text": "Updated education wording."}],
             "RESUME_REWRITE_FACT_INVALID"),
            (["other"], [{"fact_id": "experience", "text": "Updated source wording."}],
             "RESUME_REWRITE_SELECTION_INVALID"),
            (["experience"], [
                {"fact_id": "experience", "text": "First updated wording."},
                {"fact_id": "experience", "text": "Second updated wording."},
            ], "RESUME_REWRITE_SELECTION_INVALID"),
        ]
        for selected, rewrites, code in cases:
            with self.subTest(selected=selected, rewrites=rewrites):
                response = self.client.post(self.url(), json=self.request(
                    selected=selected, rewrites=rewrites,
                ))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["detail"]["code"], code)
        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
