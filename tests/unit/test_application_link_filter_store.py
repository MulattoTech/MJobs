import tempfile
import unittest
from pathlib import Path

from packages.persistence import models as m
from packages.persistence.store import Store, upsert


class ApplicationLinkFilterStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        with self.store.engine.begin() as connection:
            upsert(connection, m.companies, {
                "id": "company", "name": "Example", "domain": "example.test",
            })
            for job_id, title, status in (
                ("a-unavailable", "A unavailable", "UNAVAILABLE"),
                ("b-provider", "B provider", "PROVIDER_SUPPLIED"),
                ("c-missing", "C missing", None),
                ("d-unknown", "D unknown", "AVAILABLE"),
                ("e-provider", "E provider", "PROVIDER_SUPPLIED"),
            ):
                attributes = {"application_url_status": status} if status is not None else {}
                upsert(connection, m.jobs, {
                    "id": job_id, "company_id": "company", "requisition": job_id,
                    "title": title, "description": "", "skills": [], "location": "Unknown",
                    "remote_type": "UNKNOWN", "attributes": attributes,
                })
                upsert(connection, m.routes, {
                    "id": f"route-{job_id}", "job_id": job_id, "company_id": "company",
                    "provider": "fixture", "tenant": "example", "requisition": job_id,
                    "url": f"https://example.test/{job_id}",
                })

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_available_matches_only_provider_supplied_and_filters_before_limit(self):
        rows = self.store.jobs(sort="title", limit=1, application_link_status="AVAILABLE")
        self.assertEqual([row["id"] for row in rows], ["b-provider"])

    def test_unavailable_is_exact_and_missing_or_unknown_do_not_match(self):
        rows = self.store.jobs(sort="title", application_link_status="UNAVAILABLE")
        self.assertEqual([row["id"] for row in rows], ["a-unavailable"])

    def test_link_filter_and_composes_with_existing_job_filters(self):
        rows = self.store.jobs(
            sort="title", title="E provider", application_link_status="AVAILABLE",
        )
        self.assertEqual([row["id"] for row in rows], ["e-provider"])
        self.assertEqual(self.store.jobs(
            sort="title", title="E provider", application_link_status="UNAVAILABLE",
        ), [])

    def test_omitted_link_filter_preserves_existing_results(self):
        rows = self.store.jobs(sort="title")
        self.assertEqual(len(rows), 5)


if __name__ == "__main__":
    unittest.main()
