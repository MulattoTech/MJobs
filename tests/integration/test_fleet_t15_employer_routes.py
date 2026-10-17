"""Connected safeguards for employer feed strategy identity and routing."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select, update

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.discovery.company_feed import import_company_feed_jobs


class FleetT15FeedStrategyIdentityTests(unittest.TestCase):
    """A feed's persisted strategy is part of its canonical collection identity."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t15-feed-strategy-")
        self.root = Path(self.temp.name)
        self.database = self.root / "feed.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "feed-employer", "name": "Feed Employer", "domain": "",
            })
        self.endpoint = "https://employer.example/jobs.xml"

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def source(self, *, strategy="RSS", enabled=False):
        return upsert_company_job_source(
            self.store, company_id="feed-employer", provider="rss",
            tenant_identifier="feed-employer", feed_url=self.endpoint,
            collection_strategy=strategy, enabled=enabled,
        )

    @staticmethod
    def item():
        return {
            "id": "role-1", "title": "Network Engineer",
            "link": "/jobs/role-1", "url": "/jobs/role-1",
            "summary": "Python and network systems",
            "location": "Remote",
        }

    def import_feed(self, source, *, strategy="RSS", items=None, check=None):
        return import_company_feed_jobs(
            self.store, company_id="feed-employer", source_id=source["id"],
            provider="rss", tenant="feed-employer", endpoint=self.endpoint,
            strategy=strategy, items=[self.item()] if items is None else items,
            check=check,
        )

    def counts(self):
        tables = (m.jobs, m.routes, m.postings, m.posting_snapshots)
        with self.store.engine.connect() as connection:
            return tuple(connection.execute(
                select(func.count()).select_from(table)).scalar_one() for table in tables)

    def test_caller_strategy_mismatch_is_rejected_before_any_job_or_snapshot_write(self):
        source = self.source(strategy="RSS")
        before = self.counts()

        with self.assertRaisesRegex(ValueError, "COMPANY_FEED_SOURCE_MISMATCH"):
            self.import_feed(source, strategy="PUBLIC_JSON")

        self.assertEqual(self.counts(), before)

    def test_matching_rss_import_persists_manual_only_job_and_reads_back_after_reopen(self):
        source = self.source(strategy="RSS", enabled=False)
        self.assertFalse(source["enabled"])

        # An explicit one-time import remains valid while recurring collection is off.
        self.assertEqual(self.import_feed(source), 1)
        self.store.engine.dispose()
        reopened = Store(f"sqlite:///{self.database.as_posix()}")
        try:
            with reopened.engine.connect() as connection:
                job = connection.execute(select(m.jobs).where(
                    m.jobs.c.company_id == "feed-employer")).mappings().one()
                route = connection.execute(select(m.routes).where(
                    m.routes.c.job_id == job["id"])).mappings().one()
                posting = connection.execute(select(m.postings).where(
                    m.postings.c.job_id == job["id"])).mappings().one()
                snapshot = connection.execute(select(m.posting_snapshots).where(
                    m.posting_snapshots.c.posting_id == posting["id"])).mappings().one()
                persisted_source = connection.execute(select(m.company_sources).where(
                    m.company_sources.c.id == source["id"])).mappings().one()
            self.assertEqual(job["company_id"], "feed-employer")
            self.assertEqual(job["attributes"]["collection_strategy"], "RSS")
            self.assertEqual(job["attributes"]["submission_capability"], "MANUAL_ONLY")
            self.assertEqual(route["company_id"], job["company_id"])
            self.assertEqual(route["provider"], "rss")
            self.assertEqual(snapshot["posting_id"], posting["id"])
            self.assertEqual(persisted_source["collection_strategy"], "RSS")
            self.assertFalse(persisted_source["enabled"])
        finally:
            reopened.engine.dispose()

    def test_strategy_change_after_initial_validation_is_rejected_under_lock(self):
        source = self.source(strategy="RSS")
        from services.discovery import company_feed
        original_validate = company_feed._validate_source_identity

        def change_after_validation(*args, **kwargs):
            validated = original_validate(*args, **kwargs)
            with self.store.engine.begin() as connection:
                connection.execute(update(m.company_sources).where(
                    m.company_sources.c.id == source["id"]).values(
                        collection_strategy="PUBLIC_JSON"))
            return validated

        with patch.object(company_feed, "_validate_source_identity",
                          side_effect=change_after_validation):
            with self.assertRaisesRegex(ValueError, "COMPANY_FEED_SOURCE_CHANGED"):
                self.import_feed(source)

        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_interruption_after_posting_write_rolls_back_jobs_routes_and_snapshots(self):
        source = self.source(strategy="RSS")
        from services.discovery import company_feed
        from workers.operations import OperationCancelled
        original_persist = company_feed.persist_posting

        def interrupt_after_persist(connection, posting):
            original_persist(connection, posting)
            raise OperationCancelled("synthetic interruption")

        with patch.object(company_feed, "persist_posting", side_effect=interrupt_after_persist):
            with self.assertRaises(OperationCancelled):
                self.import_feed(source)

        self.assertEqual(self.counts(), (0, 0, 0, 0))
