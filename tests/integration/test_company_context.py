"""Company-context provenance gates use existing verified company/source records."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.sources import upsert_company_job_source
from services.company_discovery.company_context import (
    approved_company_context_for_job,
    list_company_contexts,
    review_company_context,
    save_company_context,
)


class CompanyContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'context.db').as_posix()}")
        self.store.migrate()
        self.root = Path(self.temp.name)
        self.store.bind_workspace_mode("FIXTURE")
        self.app = create_app(self.store, "http://127.0.0.1:8090", self.root,
                              operation_mode="FIXTURE")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "acme", "name": "Acme", "domain": "unverified.example",
            })
        self.source = upsert_company_job_source(
            self.store, company_id="acme", provider="rss", tenant_identifier="acme",
            career_url="https://careers.acme.example/jobs",
            feed_url="https://careers.acme.example/jobs.xml",
            collection_strategy="RSS", confidence=0.90,
        )
        self._job("acme-job", self.source["id"])

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _job(self, job_id, source_id):
        with self.store.engine.begin() as connection:
            upsert(connection, m.jobs, {
                "id": job_id, "company_id": "acme", "requisition": job_id,
                "title": "Systems Administrator", "description": "Support Linux operations.",
                "skills": ["Linux"], "location": "Unknown", "remote_type": "UNKNOWN",
                "attributes": {},
            })
            upsert(connection, m.postings, {
                "id": job_id + "-posting", "job_id": job_id,
                "source": "company-feed:" + source_id, "source_job_id": job_id,
                "source_url": "https://careers.acme.example/jobs.xml",
                "application_url": "https://careers.acme.example/jobs/1",
                "raw": {"id": job_id}, "fetched_at": "2026-10-08T00:00:00Z",
            })

    def test_user_context_stays_unverified_and_unusable_without_verified_company_domain(self):
        context = save_company_context(
            self.store, self.source["id"],
            text="Acme supports regional infrastructure operations.",
            source_url="https://careers.acme.example/about",
            request_id="unverified-company-context",
        )

        self.assertEqual(context["content_origin"], "USER_PROVIDED")
        self.assertEqual(context["content_status"], "UNVERIFIED")
        self.assertEqual(context["identity_status"], "UNVERIFIED")
        self.assertFalse(context["user_reviewed"])
        self.assertFalse(context["user_approved"])
        self.assertIsNone(approved_company_context_for_job(
            self.store, "acme-job", "acme"))
        with self.assertRaisesRegex(ValueError, "COMPANY_CONTEXT_SOURCE_UNVERIFIED"):
            review_company_context(self.store, context["id"], approved=True,
                                   content_sha256=context["content_sha256"])

    def test_wrong_company_or_source_host_cannot_be_approved(self):
        record_company_domain(self.store, "acme", "acme.example",
                              "VERIFIED_EXISTING_DOMAIN", ["synthetic verified record"])
        context = save_company_context(
            self.store, self.source["id"],
            text="Another company has 300 offices.",
            source_url="https://other-company.example/about",
            request_id="wrong-company-context-source",
        )

        self.assertEqual(context["identity_status"], "UNVERIFIED")
        self.assertEqual(context["company_id"], "acme")
        self.assertEqual(context["company_source_id"], self.source["id"])
        with self.assertRaisesRegex(ValueError, "COMPANY_CONTEXT_SOURCE_UNVERIFIED"):
            review_company_context(self.store, context["id"], approved=True,
                                   content_sha256=context["content_sha256"])

    def test_review_approval_is_required_and_exact_source_provenance_is_persisted(self):
        record_company_domain(self.store, "acme", "acme.example",
                              "VERIFIED_EXISTING_DOMAIN", ["synthetic verified record"])
        context = save_company_context(
            self.store, self.source["id"],
            text="Acme provides regional infrastructure support.",
            source_url="https://careers.acme.example/about/company",
            request_id="reviewable-company-context",
        )
        self.assertEqual(context["identity_status"], "VERIFIED")
        self.assertFalse(context["user_reviewed"])
        self.assertIsNone(approved_company_context_for_job(
            self.store, "acme-job", "acme"))

        with self.assertRaisesRegex(ValueError, "COMPANY_CONTEXT_CONTENT_CHANGED"):
            review_company_context(self.store, context["id"], approved=True,
                                   content_sha256="0" * 64)
        approved = review_company_context(
            self.store, context["id"], approved=True,
            content_sha256=context["content_sha256"],
        )

        self.assertTrue(approved["user_reviewed"])
        self.assertTrue(approved["user_approved"])
        self.assertEqual(approved["content_status"], "UNVERIFIED")
        self.assertEqual(approved["source_url"], "https://careers.acme.example/about/company")
        self.assertEqual(approved["verified_domain"], "acme.example")
        selected = approved_company_context_for_job(self.store, "acme-job", "acme")
        self.assertEqual(selected["id"], context["id"])
        self.assertEqual(selected["content_origin"], "USER_PROVIDED")
        self.assertEqual(selected["source_identity_method"], "VERIFIED_EXISTING_DOMAIN")
        persisted = list_company_contexts(self.store, self.source["id"])
        self.assertEqual(persisted[0]["content_sha256"], context["content_sha256"])

    def test_new_unreviewed_version_or_lost_unique_domain_suppresses_context(self):
        record_company_domain(self.store, "acme", "acme.example",
                              "VERIFIED_EXISTING_DOMAIN", ["synthetic verified record"])
        approved = save_company_context(
            self.store, self.source["id"], text="Acme supports regional operations.",
            source_url="https://careers.acme.example/about", request_id="approved-first-version",
        )
        review_company_context(self.store, approved["id"], approved=True,
                               content_sha256=approved["content_sha256"])
        self.assertEqual(approved_company_context_for_job(self.store, "acme-job", "acme")["id"],
                         approved["id"])

        newer = save_company_context(
            self.store, self.source["id"], text="Newer context awaits review.",
            source_url="https://careers.acme.example/about", request_id="unreviewed-second-version",
        )
        self.assertIsNone(approved_company_context_for_job(self.store, "acme-job", "acme"))
        self.assertEqual(newer["version"], approved["version"] + 1)

        review_company_context(self.store, newer["id"], approved=True,
                               content_sha256=newer["content_sha256"])
        record_company_domain(self.store, "acme", "other.example",
                              "VERIFIED_EXISTING_DOMAIN", ["second synthetic verified record"])
        self.assertEqual(list_company_contexts(self.store, self.source["id"])[0]["identity_status"],
                         "UNVERIFIED")
        self.assertIsNone(approved_company_context_for_job(self.store, "acme-job", "acme"))

    def test_local_api_save_review_and_reopen_preserve_source_context(self):
        record_company_domain(self.store, "acme", "acme.example",
                              "VERIFIED_EXISTING_DOMAIN", ["synthetic verified record"])
        with TestClient(self.app) as client:
            initial = client.get("/company-sources/view")
            self.assertEqual(initial.status_code, 200, initial.text)
            self.assertEqual(initial.json()["company_sources"][0]["company_contexts"], [])
            saved = client.post(f"/company-sources/{self.source['id']}/contexts", json={
                "text": "Acme provides regional infrastructure support.",
                "source_url": "https://careers.acme.example/about/company",
                "request_id": "api-context-save",
            })
            self.assertEqual(saved.status_code, 201, saved.text)
            context = saved.json()
            self.assertEqual(context["content_origin"], "USER_PROVIDED")
            self.assertEqual(context["content_status"], "UNVERIFIED")
            self.assertEqual(context["identity_status"], "VERIFIED")
            self.assertFalse(context["user_approved"])
            reviewed = client.post(
                f"/company-sources/{self.source['id']}/contexts/{context['id']}/review",
                json={"approved": True, "content_sha256": context["content_sha256"]},
            )
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
            self.assertTrue(reviewed.json()["user_approved"])
            visible = client.get("/company-sources/view").json()["company_sources"][0]
            self.assertEqual(visible["company_contexts"][0]["source_url"], context["source_url"])

        self.store.engine.dispose()
        reopened = Store(f"sqlite:///{(self.root / 'context.db').as_posix()}")
        try:
            readback = list_company_contexts(reopened, self.source["id"])[0]
        finally:
            reopened.engine.dispose()
        self.assertTrue(readback["user_reviewed"])
        self.assertTrue(readback["user_approved"])
        self.assertEqual(readback["content_sha256"], context["content_sha256"])


if __name__ == "__main__":
    unittest.main()
