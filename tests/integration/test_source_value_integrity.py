"""Integrity regressions for source-attributed posting values."""
from copy import deepcopy
from pathlib import Path
import unittest

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import identity, now, upsert
from services.company_discovery.domains import record_company_domain
from services.company_discovery.quality import company_source_quality, record_observation
from services.company_discovery.sources import upsert_company_job_source
from services.dedupe.source_values import record_candidate, resolve_job_values
from services.discovery.company_feed import import_company_feed_jobs
from services.discovery.sources.collect import persist_posting
from tests.integration import test_source_value_resolution as source_value_resolution


class SourceValueIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_value_resolution.SourceValueResolutionTests()
        self.fixture.setUp()
        self.store = self.fixture.store

    def tearDown(self):
        self.fixture.tearDown()

    def _row(self, guid="duplicate", title="Python Engineer", link="/jobs/duplicate"):
        return {"guid": guid, "title": title, "description": "Python engineering.",
                "location": "Remote - United States", "link": link}

    def _collect_rich(self):
        self.fixture.collect(self.fixture.rich, True)
        with self.store.engine.connect() as connection:
            posting = connection.execute(select(m.postings)).mappings().one()
            candidate = connection.execute(select(m.posting_value_candidates)).mappings().one()
            observation = connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.company_source_id == self.fixture.rich["id"]
            ).order_by(m.source_quality_observations.c.observed_at.desc())).mappings().first()
        return posting, candidate, observation

    def test_conflicting_batch_duplicate_rolls_back_and_identical_duplicate_is_allowed(self):
        self.fixture.collect(self.fixture.rich, True)
        before = self.store.job(self.fixture.job_id)
        with self.store.engine.connect() as connection:
            before_raw = connection.execute(select(m.postings.c.raw)).scalar_one()
        with self.assertRaisesRegex(ValueError, "SOURCE_DUPLICATE_JOB_ID"):
            import_company_feed_jobs(
                self.store, company_id="company", source_id=self.fixture.rich["id"],
                provider="rss", tenant="company", endpoint=self.fixture.rich["endpoint"], strategy="RSS",
                items=[self._row(title="First"), self._row(title="Second")], fetch_available=True,
            )
        after = self.store.job(self.fixture.job_id)
        self.assertEqual(after["title"], before["title"])
        with self.store.engine.connect() as connection:
            self.assertEqual(self.store.count("job_postings"), 1)
            self.assertEqual(self.store.count("posting_value_candidates"), 1)
            self.assertEqual(connection.execute(select(m.postings.c.raw)).scalar_one(), before_raw)
        imported = import_company_feed_jobs(
            self.store, company_id="company", source_id=self.fixture.rich["id"],
            provider="rss", tenant="company", endpoint=self.fixture.rich["endpoint"], strategy="RSS",
            items=[self._row(guid="same", title="Same"), self._row(guid="same", title="Same")],
            fetch_available=True,
        )
        self.assertEqual(imported, 2)

    def test_persist_posting_conflict_never_reparents_existing_posting(self):
        posting, _, _ = self._collect_rich()
        original = dict(posting)
        with self.assertRaisesRegex(ValueError, "POSTING_SOURCE_IDENTITY_CONFLICT"):
            with self.store.engine.begin() as connection:
                persist_posting(connection, {**original, "job_id": "different-job",
                    "source_job_id": "different-source-job", "raw": {"tampered": True}})
        with self.store.engine.connect() as connection:
            current = connection.execute(select(m.postings).where(m.postings.c.id == original["id"])).mappings().one()
        self.assertEqual(current["job_id"], original["job_id"])
        self.assertEqual(current["source_job_id"], original["source_job_id"])
        self.assertEqual(current["raw"], original["raw"])

    def test_candidate_rejects_wrong_source_observation_and_endpoint_without_writes(self):
        posting, candidate, observation = self._collect_rich()
        normalized = deepcopy(candidate["normalized"])
        initial_candidates = self.store.count("posting_value_candidates")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {"id": "other-company", "name": "Other", "domain": ""})
        other = upsert_company_job_source(self.store, company_id="other-company", provider="rss",
            tenant_identifier="other-company", feed_url="https://other.example/jobs.xml", collection_strategy="RSS")
        with self.assertRaisesRegex(ValueError, "SOURCE_VALUE_IDENTITY_MISMATCH"):
            with self.store.engine.begin() as connection:
                record_candidate(connection, posting_id=posting["id"], company_source_id=other["id"],
                                 observation_id=observation["id"], normalized=normalized)
        self.fixture.collect(self.fixture.sparse, False)
        with self.store.engine.connect() as connection:
            other_observation = connection.execute(select(m.source_quality_observations).where(
                m.source_quality_observations.c.company_source_id == self.fixture.sparse["id"]
            ).order_by(m.source_quality_observations.c.observed_at.desc())).mappings().first()
        with self.assertRaisesRegex(ValueError, "SOURCE_VALUE_OBSERVATION_MISMATCH"):
            with self.store.engine.begin() as connection:
                record_candidate(connection, posting_id=posting["id"], company_source_id=self.fixture.rich["id"],
                                 observation_id=other_observation["id"], normalized=normalized)
        endpoint_values = deepcopy(normalized)
        endpoint_values["attributes"]["source_endpoint"] = "https://wrong.example/jobs.xml"
        with self.assertRaisesRegex(ValueError, "SOURCE_VALUE_IDENTITY_MISMATCH"):
            with self.store.engine.begin() as connection:
                record_candidate(connection, posting_id=posting["id"], company_source_id=self.fixture.rich["id"],
                                 observation_id=observation["id"], normalized=endpoint_values)
        self.assertEqual(self.store.count("posting_value_candidates"), initial_candidates + 1)

    def test_retargeted_source_does_not_retroactively_authorize_old_candidate_or_quality(self):
        posting, _, _ = self._collect_rich()
        old_source = self.fixture.rich
        new_endpoint = "https://new.example/jobs.xml"
        record_company_domain(self.store, "company", "new.example", "FIRST_PARTY_JOB_LINK", ["verified portal"])
        retargeted = upsert_company_job_source(self.store, company_id="company", provider="rss",
            tenant_identifier="company", career_url="https://rich.example/careers",
            feed_url=new_endpoint, collection_strategy="RSS", discovered_automatically=True)
        self.assertEqual(retargeted["id"], old_source["id"])
        reference = now()
        with self.store.engine.begin() as connection:
            connection.execute(m.source_quality_observations.insert().values(
                id="historical-null-endpoint", company_source_id=old_source["id"], source_endpoint=None,
                observed_at="2026-10-02T00:00:00+00:00", succeeded=True, available=True,
                enumerated=1, accepted=1, completeness_sum=1.0, salary_known=1))
            new_quality = company_source_quality(connection, retargeted, at=reference)
        self.assertIsNone(new_quality["score"])
        with self.store.engine.begin() as connection:
            record_observation(connection, company_source_id=old_source["id"], source_endpoint=new_endpoint,
                observed_at=reference, succeeded=True, available=True,
                enumerated=1, accepted=1, completeness_sum=1.0, salary_known=1)
            resolved = resolve_job_values(connection, self.fixture.job_id,
                at=reference, seen_at=reference)
            quality = resolved["attributes"]["source_resolution"]["quality"]
        self.assertEqual(resolved["attributes"]["source_resolution"]["authority"], "UNKNOWN")
        self.assertIsNotNone(quality["score"])
        self.assertEqual(resolved["attributes"]["source_resolution"]["selected_posting_id"], posting["id"])


if __name__ == "__main__":
    unittest.main()
