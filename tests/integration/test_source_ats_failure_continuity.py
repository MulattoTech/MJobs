"""Failure continuity for bounded, supported source discovery strategies."""
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services.company_discovery.flywheel import discover_company_source
from services.company_discovery.quality import record_observation
from services.company_discovery.resolution import resolve_company
from services.company_discovery.sources import list_company_source_view, upsert_company_job_source


class SourceATSFailureContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_malformed_preferred_feed_falls_back_without_hiding_failure_or_stopping_sibling(self):
        primary_origin = "https://primary.example"
        sibling_origin = "https://sibling.example"
        primary_candidate = {
            "company_name": "Primary Employer",
            "company_website": primary_origin + "/",
        }
        primary_company = resolve_company(self.store, primary_candidate)
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": primary_company["company_id"],
                "name": primary_company["name"],
                "domain": "",
            })
        preferred = upsert_company_job_source(
            self.store,
            company_id=primary_company["company_id"],
            provider="json_feed",
            tenant_identifier=primary_company["company_id"],
            career_url=primary_origin + "/careers",
            public_api_url=primary_origin + "/jobs.json",
            collection_strategy="PUBLIC_JSON",
            confidence=0.88,
            discovered_automatically=True,
        )

        pages = {
            primary_origin + "/": SimpleNamespace(
                url=primary_origin + "/",
                text='<a href="/careers">Careers</a>',
            ),
            primary_origin + "/careers": SimpleNamespace(
                url=primary_origin + "/careers",
                text=(
                    '<link rel="alternate" type="application/feed+json" href="/jobs.json">'
                    '<link rel="alternate" type="application/rss+xml" href="/jobs.xml">'
                ),
            ),
            primary_origin + "/jobs.json": SimpleNamespace(
                url=primary_origin + "/jobs.json",
                text='{"version":"https://jsonfeed.org/version/1.1","items":',
            ),
            primary_origin + "/jobs.xml": SimpleNamespace(
                url=primary_origin + "/jobs.xml",
                text=(
                    '<rss version="2.0"><channel><item><guid>primary-role</guid>'
                    '<title>Primary Engineer</title><link>/jobs/primary-role</link>'
                    '<description>Python services</description></item></channel></rss>'
                ),
            ),
            sibling_origin + "/": SimpleNamespace(
                url=sibling_origin + "/",
                text='<a href="/careers">Careers</a>',
            ),
            sibling_origin + "/careers": SimpleNamespace(
                url=sibling_origin + "/careers",
                text='<link rel="alternate" type="application/feed+json" href="/jobs.json">',
            ),
            sibling_origin + "/jobs.json": SimpleNamespace(
                url=sibling_origin + "/jobs.json",
                text=(
                    '{"version":"https://jsonfeed.org/version/1.1","items":['
                    '{"id":"sibling-role","title":"Sibling Engineer",'
                    '"url":"https://sibling.example/jobs/sibling-role",'
                    '"content_text":"Python services"}]}'
                ),
            ),
        }

        class FixtureTransport:
            bounded_redirects = True
            fixture_only = True

            def __init__(self):
                self.urls = []

            def allowed(self, url):
                return url in pages

            def get(self, url):
                self.urls.append(url)
                if url not in pages:
                    raise AssertionError(f"unsupported fixture fetch: {url}")
                return pages[url]

        transport = FixtureTransport()
        primary_result = discover_company_source(
            self.store, primary_candidate, transport,
        )
        sibling_result = discover_company_source(self.store, {
            "company_name": "Sibling Employer",
            "company_website": sibling_origin + "/",
        }, transport)

        attempts = Counter(transport.urls)
        self.assertEqual(attempts[primary_origin + "/jobs.json"], 1)
        self.assertEqual(attempts[primary_origin + "/jobs.xml"], 1)
        self.assertEqual(attempts[sibling_origin + "/jobs.json"], 1)
        self.assertEqual(set(attempts), set(pages))
        self.assertEqual(primary_result["provider"], "rss")
        self.assertEqual(primary_result["jobs_imported"], 1)
        self.assertEqual(sibling_result["provider"], "json_feed")
        self.assertEqual(sibling_result["jobs_imported"], 1)
        self.assertEqual(
            {row["requisition"] for row in self.store.jobs()},
            {"primary-role", "sibling-role"},
        )
        self.assertEqual(self.store.count("applications"), 0)

        self.store.engine.dispose()
        reopened = Store(self.url)
        with TestClient(create_app(
            reopened,
            "http://127.0.0.1:8090",
            self.root,
            operation_mode="FIXTURE",
        )) as client:
            registry = client.get(
                "/company-sources",
                params={"company_id": primary_company["company_id"]},
            )
            registry.raise_for_status()
            source_view = client.get("/company-sources/view")
            source_view.raise_for_status()

        with reopened.engine.connect() as connection:
            preferred_failure = connection.execute(
                m.source_quality_observations.select().where(
                    m.source_quality_observations.c.company_source_id == preferred["id"]
                )
            ).mappings().one()
        reopened.engine.dispose()

        registered_preferred = next(
            row for row in registry.json()["company_sources"]
            if row["id"] == preferred["id"]
        )
        self.assertEqual(registered_preferred, preferred)
        preferred_view = next(
            row for row in source_view.json()["company_sources"]
            if row["id"] == preferred["id"]
        )
        sibling_view = next(
            row for row in source_view.json()["company_sources"]
            if row["id"] == sibling_result["company_source_id"]
        )
        self.assertFalse(preferred_failure["available"])
        self.assertEqual(preferred_failure["error_code"], "SOURCE_UNAVAILABLE")
        self.assertEqual(preferred_view["status"], "COMPANY_SOURCE_REVIEW_REQUIRED")
        self.assertEqual(preferred_view["quality"]["components"]["availability"], 0.0)
        self.assertEqual(preferred_view["quality"]["sample_counts"]["availability"], 1)
        self.assertEqual(sibling_view["status"], "ENABLED")
        self.assertEqual(sibling_view["quality"]["components"]["availability"], 100.0)
        self.assertNotIn("ValueError", source_view.text)

    def test_aged_out_latest_failure_stays_review_required_until_later_success(self):
        company = resolve_company(self.store, {
            "company_name": "Historical Failure Employer",
            "company_website": "https://historical.example/",
        })
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": company["company_id"],
                "name": company["name"],
                "domain": "",
            })
        source = upsert_company_job_source(
            self.store,
            company_id=company["company_id"],
            provider="json_feed",
            tenant_identifier=company["company_id"],
            career_url="https://historical.example/careers",
            public_api_url="https://historical.example/jobs.json",
            collection_strategy="PUBLIC_JSON",
            confidence=0.88,
            discovered_automatically=True,
        )
        with self.store.engine.begin() as connection:
            record_observation(
                connection,
                company_source_id=source["id"],
                source_endpoint=source["endpoint"],
                observed_at="2000-01-01T00:00:00Z",
                succeeded=False,
                available=False,
                error_code="SOURCE_UNAVAILABLE",
            )

        failed_view = next(
            row for row in list_company_source_view(self.store)
            if row["id"] == source["id"]
        )
        self.assertIsNone(failed_view["quality"]["components"]["availability"])
        self.assertEqual(failed_view["status"], "COMPANY_SOURCE_REVIEW_REQUIRED")

        with self.store.engine.begin() as connection:
            record_observation(
                connection,
                company_source_id=source["id"],
                source_endpoint=source["endpoint"],
                observed_at="2000-01-02T00:00:00Z",
                succeeded=True,
                available=True,
            )

        recovered_view = next(
            row for row in list_company_source_view(self.store)
            if row["id"] == source["id"]
        )
        self.assertIsNone(recovered_view["quality"]["components"]["availability"])
        self.assertEqual(recovered_view["status"], "ENABLED")


if __name__ == "__main__":
    unittest.main()
