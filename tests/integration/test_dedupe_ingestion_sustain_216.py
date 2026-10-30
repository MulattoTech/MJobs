import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs


class DedupeIngestionSustain216Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'dedupe.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "company",
                "name": "Synthetic Employer",
                "domain": "example.test",
            })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def source(self, provider, tenant, origin):
        return upsert_company_job_source(
            self.store,
            company_id="company",
            provider=provider,
            tenant_identifier=tenant,
            feed_url=f"{origin}/jobs.xml",
            collection_strategy="RSS",
            discovered_automatically=True,
        )

    def ingest(self, source, *, requisition, application_url, title="Platform Engineer",
               description="Build and operate synthetic platform services.", location="Dallas, TX"):
        return import_company_feed_jobs(
            self.store,
            company_id="company",
            source_id=source["id"],
            provider=source["provider"],
            tenant=source["tenant"],
            endpoint=source["endpoint"],
            strategy="RSS",
            items=[{
                "guid": requisition,
                "title": title,
                "description": description,
                "location": location,
                "link": application_url,
            }],
            fetch_available=True,
        )

    def persisted_identity(self):
        with self.store.engine.connect() as connection:
            jobs = [dict(row) for row in connection.execute(
                select(m.jobs).order_by(m.jobs.c.id)
            ).mappings()]
            postings = [dict(row) for row in connection.execute(
                select(m.postings).order_by(m.postings.c.id)
            ).mappings()]
        return jobs, postings

    def reopen(self):
        self.store.engine.dispose()
        self.store = Store(self.url)

    def test_exact_replay_and_same_ats_requisition_keep_durable_posting_identity(self):
        first = self.source("rss", "acme", "https://one.example.test")
        second = self.source("rss", "acme", "https://two.example.test")
        application_url = "https://apply.example.test/jobs/REQ-100"

        self.assertEqual(self.ingest(
            first, requisition="REQ-100", application_url=application_url), 1)
        self.assertEqual(self.ingest(
            first, requisition="REQ-100", application_url=application_url), 1)
        self.assertEqual(self.ingest(
            second, requisition="REQ-100", application_url=application_url), 1)

        jobs_before, postings_before = self.persisted_identity()
        self.assertEqual(len(jobs_before), 1)
        self.assertEqual(len(postings_before), 2)
        self.assertEqual({row["job_id"] for row in postings_before}, {jobs_before[0]["id"]})
        self.assertEqual(len({row["source"] for row in postings_before}), 2)
        self.assertEqual(len({row["source_job_id"] for row in postings_before}), 2)

        self.reopen()
        jobs_after, postings_after = self.persisted_identity()
        self.assertEqual(jobs_after, jobs_before)
        self.assertEqual(postings_after, postings_before)

    def test_cross_provider_exact_route_attaches_to_one_canonical_job_after_reopen(self):
        employer = self.source("rss", "employer", "https://employer.example.test")
        aggregator = self.source("feed", "aggregator", "https://board.example.test")
        application_url = "https://apply.example.test/jobs/REQ-200"

        self.ingest(employer, requisition="REQ-200", application_url=application_url)
        self.ingest(
            aggregator,
            requisition="listing-999",
            application_url=application_url,
            title="Platform Systems Engineer",
            description="Build and operate synthetic platform systems.",
        )

        jobs_before, postings_before = self.persisted_identity()
        self.assertEqual(len(jobs_before), 1)
        self.assertEqual(len(postings_before), 2)
        self.assertEqual({row["job_id"] for row in postings_before}, {jobs_before[0]["id"]})
        self.assertEqual(len({row["source"] for row in postings_before}), 2)

        self.reopen()
        jobs_after, postings_after = self.persisted_identity()
        self.assertEqual(jobs_after, jobs_before)
        self.assertEqual(postings_after, postings_before)

    def test_distinct_application_routes_remain_distinct_after_reopen(self):
        source = self.source("rss", "acme", "https://employer.example.test")

        self.ingest(
            source,
            requisition="REQ-DALLAS",
            application_url="https://apply.example.test/jobs/REQ-DALLAS",
            location="Dallas, TX",
        )
        self.ingest(
            source,
            requisition="REQ-AUSTIN",
            application_url="https://apply.example.test/jobs/REQ-AUSTIN",
            location="Austin, TX",
        )

        jobs_before, postings_before = self.persisted_identity()
        self.assertEqual(len(jobs_before), 2)
        self.assertEqual(len(postings_before), 2)
        self.assertEqual(len({row["job_id"] for row in postings_before}), 2)

        self.reopen()
        jobs_after, postings_after = self.persisted_identity()
        self.assertEqual(jobs_after, jobs_before)
        self.assertEqual(postings_after, postings_before)


if __name__ == "__main__":
    unittest.main()
