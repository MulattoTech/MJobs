"""Company-feed quality observation and source attribution boundaries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.sources import upsert_company_job_source
from services.company_discovery.quality import record_observation
from services.discovery.company_feed import import_company_feed_jobs
from tests.integration.test_spine import PROFILE


class CompanyFeedQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(PROFILE)
        self.endpoint = "https://employer.example/jobs.json"
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "company", "name": "Employer", "domain": "",
            })
        self.source = upsert_company_job_source(
            self.store, company_id="company", provider="rss", tenant_identifier="tenant",
            public_api_url=self.endpoint, collection_strategy="PUBLIC_JSON",
        )

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _item(self, *, link="/jobs/1", title="Network Administrator", description="Python systems", salary=True):
        return {
            "id": "job-1", "title": title, "description": description,
            "location": "Remote", "url": link,
            **({"salary": "$90,000 per year"} if salary else {}),
        }

    def test_source_identity_mismatch_writes_nothing(self):
        with patch("services.discovery.company_feed.record_observation", wraps=record_observation) as observe:
            with self.assertRaisesRegex(ValueError, "COMPANY_FEED_SOURCE_MISMATCH"):
                import_company_feed_jobs(
                    self.store, company_id="other-company", source_id=self.source["id"],
                    provider="rss", tenant="tenant", endpoint=self.endpoint,
                    strategy="PUBLIC_JSON", items=[self._item()],
                )
        observe.assert_not_called()
        self.assertEqual(self.store.count("canonical_jobs"), 0)
        self.assertEqual(self.store.count("audit_events"), 1)

    def test_canonical_equivalent_endpoint_preserves_relative_url_resolution(self):
        with patch("services.discovery.company_feed.record_observation", wraps=record_observation):
            imported = import_company_feed_jobs(
                self.store, company_id="company", source_id=self.source["id"],
                provider="RSS", tenant="tenant", endpoint="HTTPS://EMPLOYER.EXAMPLE:443/jobs.json/",
                strategy="PUBLIC_JSON", items=[self._item(link="relative")],
            )
        self.assertEqual(imported, 1)
        with self.store.engine.connect() as connection:
            route = connection.execute(m.routes.select().where(m.routes.c.company_id == "company")).mappings().one()
        self.assertEqual(route["url"], "https://EMPLOYER.EXAMPLE:443/jobs.json/relative")

    def test_different_endpoint_path_or_query_is_rejected(self):
        for endpoint in ("https://employer.example/other.json", "https://employer.example/jobs.json?other=1"):
            with self.subTest(endpoint=endpoint), patch("services.discovery.company_feed.record_observation", wraps=record_observation) as observe:
                with self.assertRaisesRegex(ValueError, "COMPANY_FEED_SOURCE_MISMATCH"):
                    import_company_feed_jobs(
                        self.store, company_id="company", source_id=self.source["id"],
                        provider="rss", tenant="tenant", endpoint=endpoint,
                        strategy="PUBLIC_JSON", items=[self._item()],
                    )
                observe.assert_not_called()

    def test_partial_rows_record_enumerated_and_accepted_metrics(self):
        items = [self._item(), "malformed-row", self._item(link="", title="Other Role"),
                 self._item(link="/jobs/2", title="")]
        with patch("services.discovery.company_feed.record_observation", wraps=record_observation) as observe:
            imported = import_company_feed_jobs(
                self.store, company_id="company", source_id=self.source["id"],
                provider="rss", tenant="tenant", endpoint=self.endpoint,
                strategy="PUBLIC_JSON", items=items,
            )
        self.assertEqual(imported, 1)
        kwargs = observe.call_args.kwargs
        self.assertEqual((kwargs["succeeded"], kwargs["enumerated"], kwargs["accepted"]),
                         (True, 4, 1))
        self.assertEqual(kwargs["links_checked"], None)
        self.assertEqual(kwargs["links_accurate"], None)
        self.assertGreaterEqual(kwargs["completeness_sum"], 0)
        self.assertEqual(kwargs["salary_known"], 0)

    def test_empty_feed_is_a_success_with_zero_denominator_metrics(self):
        with patch("services.discovery.company_feed.record_observation", wraps=record_observation) as observe:
            self.assertEqual(import_company_feed_jobs(
                self.store, company_id="company", source_id=self.source["id"],
                provider="rss", tenant="tenant", endpoint=self.endpoint,
                strategy="PUBLIC_JSON", items=[], fetch_available=True,
            ), 0)
        kwargs = observe.call_args.kwargs
        self.assertTrue(kwargs["succeeded"])
        self.assertTrue(kwargs["available"])
        self.assertEqual((kwargs["enumerated"], kwargs["accepted"], kwargs["completeness_sum"]), (0, 0, 0.0))

    def test_failure_is_observed_after_rollback_and_retry_can_reopen(self):
        def fail_once():
            raise RuntimeError("synthetic failure")

        with patch("services.discovery.company_feed.record_observation", wraps=record_observation) as observe:
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                import_company_feed_jobs(
                    self.store, company_id="company", source_id=self.source["id"],
                    provider="rss", tenant="tenant", endpoint=self.endpoint,
                    strategy="PUBLIC_JSON", items=[self._item()], check=fail_once,
                )
            self.assertEqual(self.store.count("canonical_jobs"), 0)
            self.assertEqual(observe.call_args.kwargs["succeeded"], False)
            self.assertIsNone(observe.call_args.kwargs["available"])
            self.assertEqual(observe.call_args.kwargs["error_code"], "RuntimeError")
            self.assertIsNone(observe.call_args.kwargs["accepted"])

            self.assertEqual(import_company_feed_jobs(
                self.store, company_id="company", source_id=self.source["id"],
                provider="rss", tenant="tenant", endpoint=self.endpoint,
                strategy="PUBLIC_JSON", items=[self._item()],
            ), 1)
        self.assertEqual(self.store.count("canonical_jobs"), 1)
        self.assertEqual(observe.call_args.kwargs["succeeded"], True)
        self.assertEqual(observe.call_args.kwargs["accepted"], 1)


if __name__ == "__main__":
    unittest.main()
