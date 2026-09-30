"""The real local HTTP flywheel and durable preparation behavior."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import hashlib
import tempfile
import unittest

from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from sqlalchemy import select

PROFILE = {"id": "fixture", "name": "Alex Fixture", "email": "alex@example.test", "answers": [
    {"id": key, "text": value, "source": "fictional-test-profile", "verified": True, "user_approved": True, "confidence": 100}
    for key, value in (("country", "United States"), ("remote", "yes"), ("fixture_question", "Controlled fictional test application."))
], "facts": [
    {"id": "python", "text": "Python", "kind": "skill", "source": "fictional-test-profile", "verified": True, "user_approved": True, "confidence": 100},
    {"id": "sql", "text": "SQL", "kind": "skill", "source": "fictional-test-profile", "verified": True, "user_approved": True, "confidence": 100},
]}


class SpineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.save_profile(PROFILE)

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_flywheel_enrolls_company_and_finds_employer_only_job(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            discover(self.store, origin, "fixture")
        jobs = self.store.jobs("fixture")
        self.assertEqual({job["requisition"] for job in jobs}, {"A", "B"})
        self.assertEqual(self.store.count("job_postings"), 4)
        self.assertEqual(self.store.count("company_job_sources"), 1)
        self.assertEqual(next(job for job in jobs if job["requisition"] == "B")["match_score"], 100)
        self.assertEqual(next(job for job in jobs if job["requisition"] == "A")["posting_count"], 3)

    def test_preparation_provenance_and_idempotency_survive_restart(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(job for job in self.store.jobs() if job["requisition"] == "B")
            first = prepare(self.store, job["id"], "fixture", self.root)
            second = prepare(self.store, job["id"], "fixture", self.root)
        self.assertEqual(first, second)
        reopened = Store(self.url)
        try:
            application = reopened.application(first)
        finally:
            reopened.engine.dispose()
        self.assertEqual(application["state"], "PREPARED")
        self.assertEqual(application["fact_ids"], ["python", "sql"])
        self.assertEqual(self.store.count("applications"), 1)
        self.assertEqual(self.store.count("documents"), 2)
        document_root = self.root / first / application["profile_version"]
        self.assertTrue((document_root / "resume.pdf").exists())
        from docx import Document
        text = "\n".join(p.text for p in Document(document_root / "resume.docx").paragraphs)
        self.assertIn("Python", text)
        self.assertNotIn("AWS Lambda", text)

    def test_unapproved_facts_cannot_prepare_an_application(self):
        bad = dict(PROFILE, id="unapproved", facts=[dict(fact, user_approved=False) for fact in PROFILE["facts"]])
        self.store.save_profile(bad)
        with fixture_server() as origin:
            discover(self.store, origin, "unapproved")
            with self.assertRaises(ValueError):
                prepare(self.store, self.store.jobs()[0]["id"], "unapproved", self.root)
        self.assertEqual(self.store.count("applications"), 0)

    def test_scores_are_scoped_to_selected_profile(self):
        bad = dict(PROFILE, id="unapproved", facts=[dict(fact, user_approved=False) for fact in PROFILE["facts"]])
        self.store.save_profile(bad)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            discover(self.store, origin, "unapproved")
        self.assertEqual({job["match_score"] for job in self.store.jobs("fixture")}, {100})
        self.assertEqual({job["match_score"] for job in self.store.jobs("unapproved")}, {0})
        self.assertEqual({job["match_score"] for job in self.store.jobs()}, {None})

    def test_concurrent_preparation_preserves_one_application_and_document_hashes(self):
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            identifiers = list(pool.map(lambda _: prepare(self.store, job["id"], "fixture", self.root), range(2)))
        self.assertEqual(identifiers[0], identifiers[1])
        self.assertEqual(self.store.count("applications"), 1)
        with self.store.engine.connect() as connection:
            documents = list(connection.execute(select(m.documents)).mappings())
        self.assertEqual(len(documents), 2)
        for document in documents:
            for extension, filename in document["files"].items():
                self.assertEqual(hashlib.sha256((self.root / identifiers[0] / filename).read_bytes()).hexdigest(), document["sha256"][extension])


if __name__ == "__main__":
    unittest.main()
