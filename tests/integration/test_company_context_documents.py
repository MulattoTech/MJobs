"""Company context in letters is optional, reviewed, and provenance-attributed."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.company_context import review_company_context, save_company_context
from services.company_discovery.domains import record_company_domain
from services.company_discovery.sources import upsert_company_job_source
from services.documents.cover_letter import build_cover_letter
from services.documents.generate import prepare
from services.documents.history import application_documents
from tests.integration.test_spine import PROFILE


def job_with_company_context(context=None):
    attributes = {"required_skills": ["Python"], "skill_evidence": "description_mentions"}
    if context is not None:
        attributes["approved_company_context"] = context
    return {
        "title": "Systems Administrator", "company": "Acme",
        "skills": ["Python"], "description": "Responsibilities:\nMaintain Linux systems.",
        "attributes": attributes,
    }


def approved_context():
    content = "Acme provides regional infrastructure support."
    return {
        "id": "context-1", "company_id": "acme", "company_source_id": "source-1",
        "version": 1, "content_origin": "USER_PROVIDED", "content_status": "UNVERIFIED",
        "content": content,
        "source_url": "https://careers.acme.example/about/company",
        "source_host": "careers.acme.example", "verified_domain": "acme.example",
        "source_identity_method": "VERIFIED_EXISTING_DOMAIN",
        "identity_status": "VERIFIED", "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "user_reviewed": True, "user_approved": True,
        "reviewed_at": "2026-10-08T00:00:00Z",
    }


class CompanyContextDocumentTests(unittest.TestCase):
    def test_letter_includes_only_reviewed_context_and_preserves_source_snapshot(self):
        paragraphs, context = build_cover_letter(
            PROFILE, job_with_company_context(approved_context()), PROFILE["facts"])

        attribution = next(value for value in paragraphs if "Company context" in value)
        self.assertIn("USER_PROVIDED", attribution)
        self.assertIn("CONTENT UNVERIFIED", attribution)
        self.assertIn("SOURCE IDENTITY VERIFIED", attribution)
        self.assertIn("https://careers.acme.example/about/company", attribution)
        self.assertIn("Acme provides regional infrastructure support.", attribution)
        self.assertEqual(context["company_context"]["id"], "context-1")
        self.assertEqual(context["company_context"]["content_sha256"], approved_context()["content_sha256"])
        self.assertEqual(context["job_snapshot"]["company_context"], approved_context())

    def test_letter_omits_absent_unverified_or_unapproved_context(self):
        unverified = approved_context() | {"identity_status": "UNVERIFIED"}
        unapproved = approved_context() | {"user_approved": False}
        for supplied in (None, unverified, unapproved):
            with self.subTest(supplied=supplied is not None):
                paragraphs, context = build_cover_letter(
                    PROFILE, job_with_company_context(supplied), PROFILE["facts"])
                self.assertFalse(any("Company context" in value for value in paragraphs))
                self.assertIsNone(context["company_context"])
                self.assertIsNone(context["job_snapshot"]["company_context"])

    def test_approved_context_is_snapshotted_in_generated_letter_after_database_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(f"sqlite:///{(root / 'documents.db').as_posix()}")
            store.migrate()
            store.save_profile(PROFILE)
            with store.engine.begin() as connection:
                upsert(connection, m.companies, {
                    "id": "acme", "name": "Acme", "domain": "acme.example",
                })
                upsert(connection, m.jobs, {
                    "id": "job-context", "company_id": "acme", "requisition": "REQ-1",
                    "title": "Systems Administrator",
                    "description": "Responsibilities:\nMaintain Python systems.",
                    "skills": ["Python"], "location": "Remote", "remote_type": "REMOTE",
                    "attributes": {"required_skills": ["Python"]},
                })
                upsert(connection, m.routes, {
                    "id": "route-context", "job_id": "job-context", "company_id": "acme",
                    "provider": "rss", "tenant": "acme", "requisition": "REQ-1",
                    "url": "https://careers.acme.example/jobs/1",
                })
            source = upsert_company_job_source(
                store, company_id="acme", provider="rss", tenant_identifier="acme",
                career_url="https://careers.acme.example/jobs",
                feed_url="https://careers.acme.example/jobs.xml",
                collection_strategy="RSS", confidence=0.90,
            )
            with store.engine.begin() as connection:
                upsert(connection, m.postings, {
                    "id": "job-context-posting", "job_id": "job-context",
                    "source": "company-feed:" + source["id"], "source_job_id": "REQ-1",
                    "source_url": "https://careers.acme.example/jobs.xml",
                    "application_url": "https://careers.acme.example/jobs/1",
                    "raw": {"id": "REQ-1"}, "fetched_at": "2026-10-08T00:00:00Z",
                })
            record_company_domain(store, "acme", "acme.example",
                                  "VERIFIED_EXISTING_DOMAIN", ["synthetic verified record"])
            context = save_company_context(
                store, source["id"], text="Acme supports regional infrastructure operations.",
                source_url="https://careers.acme.example/about/company",
                request_id="document-context-save",
            )
            review_company_context(store, context["id"], approved=True,
                                   content_sha256=context["content_sha256"])

            try:
                application_id = prepare(store, "job-context", "fixture", root)
                revised_context = save_company_context(
                    store, source["id"],
                    text="Acme supports regional infrastructure and identity systems.",
                    source_url="https://careers.acme.example/about/company",
                    request_id="document-context-revision",
                )
                review_company_context(store, revised_context["id"], approved=True,
                                       content_sha256=revised_context["content_sha256"])
                regenerated_id = prepare(store, "job-context", "fixture", root)
            finally:
                store.engine.dispose()
            self.assertEqual(regenerated_id, application_id)
            reopened = Store(f"sqlite:///{(root / 'documents.db').as_posix()}")
            try:
                documents = application_documents(reopened, application_id, root)
            finally:
                reopened.engine.dispose()

        letters = {row["version"]: row for row in documents if row["kind"] == "cover-letter"}
        self.assertEqual(set(letters), {1, 2})
        previous, letter = letters[1], letters[2]
        self.assertEqual(previous["generation_context"]["company_context"]["id"], context["id"])
        self.assertEqual(previous["generation_context"]["job_snapshot"]["company_context"]["source_url"],
                         context["source_url"])
        self.assertIn("Acme supports regional infrastructure operations.", previous["preview_text"])
        self.assertEqual(letter["generation_context"]["company_context"]["id"], revised_context["id"])
        self.assertEqual(letter["generation_context"]["job_snapshot"]["company_context"]["source_url"],
                         context["source_url"])
        self.assertIn("Company context (USER_PROVIDED; CONTENT UNVERIFIED; SOURCE IDENTITY VERIFIED; reviewed by you; source:",
                      letter["preview_text"])
        self.assertIn("Acme supports regional infrastructure and identity systems.", letter["preview_text"])
        self.assertIsNone(letter["preview_error"])


if __name__ == "__main__":
    unittest.main()
