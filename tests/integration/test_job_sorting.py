"""Connected API acceptance for the persisted Newest job sort."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from tests.integration.test_spine import PROFILE


class JobSortingApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-job-sorting-api-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        jobs = self.store.jobs(profile_id="fixture")
        self.job_ids = sorted(job["id"] for job in jobs)
        self.by_id = {job["id"]: job for job in jobs}
        self.app = create_app(self.store, "http://127.0.0.1:1", self.root,
                              "redis://127.0.0.1:1/0", operation_mode="FIXTURE")

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def _set_attributes(self, values):
        with self.store.engine.begin() as connection:
            for job_id, changes in values.items():
                attributes = dict(self.by_id[job_id]["attributes"])
                attributes.update(changes)
                connection.execute(update(m.jobs).where(m.jobs.c.id == job_id)
                                   .values(attributes=attributes))

    def _set_company_names(self, values):
        with self.store.engine.begin() as connection:
            for job_id, name in values.items():
                company_id = f"sort-company-{job_id}"
                existing = connection.execute(select(m.companies.c.id).where(
                    m.companies.c.id == company_id
                )).first()
                if existing:
                    connection.execute(update(m.companies).where(m.companies.c.id == company_id)
                                       .values(name=name))
                else:
                    connection.execute(insert(m.companies).values(
                        id=company_id, name=name, domain="", parent_company_id=None, relationship_type=None,
                    ))
                    connection.execute(update(m.jobs).where(m.jobs.c.id == job_id)
                                       .values(company_id=company_id))

    def _salary_rows(self):
        """Add intentionally varied salary metadata without calling a live source."""
        specs = [
            ("range", {"salary_min": 80000, "salary_max": 120000, "salary_period": "YEAR", "salary_currency": "USD"}),
            ("low_endpoint", {"salary_min": 95000, "salary_max": None, "salary_period": "YEAR", "salary_currency": "USD"}),
            ("high_endpoint", {"salary_min": None, "salary_max": 95000, "salary_period": "YEAR", "salary_currency": "USD"}),
            ("missing", {}),
            ("invalid", {"salary_min": "not-a-number", "salary_max": "90000", "salary_period": "YEAR", "salary_currency": "USD"}),
            ("inverted", {"salary_min": 110000, "salary_max": 90000, "salary_period": "YEAR", "salary_currency": "USD"}),
            ("hourly", {"salary_min": 80, "salary_max": 100, "salary_period": "HOUR", "salary_currency": "USD"}),
            ("currency", {"salary_min": 200000, "salary_max": 250000, "salary_period": "YEAR", "salary_currency": "CAD"}),
        ]
        with self.store.engine.begin() as connection:
            for name, salary in specs:
                company_id = f"salary-company-{name}"
                job_id = f"salary-job-{name}"
                connection.execute(insert(m.companies).values(
                    id=company_id, name="Salary fixture", domain="", parent_company_id=None,
                    relationship_type=None,
                ))
                connection.execute(insert(m.jobs).values(
                    id=job_id, company_id=company_id, requisition=job_id, title=f"Salary {name}",
                    description="Synthetic salary sorting fixture", skills=[], location="Remote",
                    remote_type="REMOTE", attributes=salary,
                ))
                connection.execute(insert(m.routes).values(
                    id=f"salary-route-{name}", job_id=job_id, company_id=company_id,
                    provider="mock-ats", tenant="salary-fixture", requisition=job_id,
                    url="https://example.invalid/apply",
                ))
        return [f"salary-job-{name}" for name, _ in specs]

    def test_salary_sort_uses_highest_disclosed_annual_usd_endpoint_and_keeps_unknown_last(self):
        ids = self._salary_rows()
        range_id, low_id, high_id, missing_id, invalid_id, inverted_id, hourly_id, currency_id = ids
        with TestClient(self.app) as client:
            high = client.get("/jobs", params={"sort": "salary_high", "title": "Salary"})
            low = client.get("/jobs", params={"sort": "salary_low", "title": "Salary"})
            limited = client.get("/jobs", params={"sort": "salary_high", "title": "Salary", "limit": 1})
            self.assertEqual(high.status_code, 200, high.text)
            self.assertEqual(low.status_code, 200, low.text)
            self.assertEqual([row["id"] for row in high.json()["jobs"][:3]],
                             [range_id, high_id, low_id])
            self.assertEqual([row["id"] for row in low.json()["jobs"][:3]],
                             [high_id, low_id, range_id])
            for response in (high, low):
                ordered = [row["id"] for row in response.json()["jobs"]]
                self.assertEqual(ordered[-5:], sorted([missing_id, invalid_id, inverted_id, hourly_id, currency_id]))
            self.assertEqual([row["id"] for row in limited.json()["jobs"]], [range_id])
            self.assertEqual(client.get("/jobs", params={"sort": "salary"}).status_code, 422)

    def test_salary_sort_is_saved_profile_scoped_and_restored_after_store_reopen(self):
        payload = {"id": "salary-sort", "profile_id": "fixture", "name": "Salary order",
                   "enabled": False, "include_titles": [], "remote": "ANY", "employment_types": [],
                   "source_ids": [], "job_filters": {"q": "", "remote_type": "", "provider": "",
                                                        "sort": "salary_high"}}
        with TestClient(self.app) as client:
            saved = client.put("/search-profiles", json=payload)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(client.get("/search-profiles", params={"profile_id": "other"})
                             .json()["search_profiles"], [])
            invalid = {**payload, "id": "bad-sort", "job_filters": {**payload["job_filters"],
                                                                        "sort": "salary"}}
            self.assertEqual(client.put("/search-profiles", json=invalid).status_code, 422)
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        reopened = create_app(self.store, "http://127.0.0.1:1", self.root,
                              "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        with TestClient(reopened) as client:
            row = next(item for item in client.get("/search-profiles", params={"profile_id": "fixture"})
                       .json()["search_profiles"] if item["id"] == "salary-sort")
            self.assertEqual(row["criteria"]["job_filters"]["sort"], "salary_high")
            self.assertEqual(client.get("/jobs", params={"profile_id": "fixture",
                                                           "search_profile_id": "salary-sort"}).status_code, 200)

    def test_newest_orders_matching_rows_before_limit_without_changing_existing_sorts(self):
        old_id, new_id = self.job_ids
        self._set_attributes({
            old_id: {"reported_posted_at": "2026-10-01T00:00:00Z"},
            new_id: {"reported_posted_at": "2026-10-07T00:00:00Z"},
        })
        with self.store.engine.begin() as connection:
            connection.execute(update(m.jobs).where(m.jobs.c.id == old_id).values(title="Alpha Old"))
            connection.execute(update(m.jobs).where(m.jobs.c.id == new_id).values(title="Zulu New"))
            before_matches = [dict(row) for row in connection.execute(
                select(m.matches).where(m.matches.c.profile_id == "fixture").order_by(m.matches.c.job_id)
            ).mappings()]

        with TestClient(self.app) as client:
            newest = client.get("/jobs", params={"profile_id": "fixture", "sort": "newest", "limit": 1})
            self.assertEqual(newest.status_code, 200, newest.text)
            self.assertEqual([row["id"] for row in newest.json()["jobs"]], [new_id])
            all_newest = client.get("/jobs", params={"profile_id": "fixture", "sort": "newest"})
            self.assertEqual([row["id"] for row in all_newest.json()["jobs"]], [new_id, old_id])
            by_title = client.get("/jobs", params={"profile_id": "fixture", "sort": "title"})
            self.assertEqual([row["id"] for row in by_title.json()["jobs"]], [old_id, new_id])
            by_score = client.get("/jobs", params={"profile_id": "fixture", "sort": "score"})
            self.assertEqual([row["id"] for row in by_score.json()["jobs"]], [old_id, new_id])

        with self.store.engine.connect() as connection:
            after_matches = [dict(row) for row in connection.execute(
                select(m.matches).where(m.matches.c.profile_id == "fixture").order_by(m.matches.c.job_id)
            ).mappings()]
        self.assertEqual(after_matches, before_matches)

    def test_newest_uses_first_seen_for_unparseable_posted_time_and_places_unknown_last(self):
        fallback_id, unknown_id = self.job_ids
        self._set_attributes({
            fallback_id: {"reported_posted_at": "not-a-date", "first_seen_at": "2026-10-05T12:00:00Z"},
            unknown_id: {"reported_posted_at": "also-not-a-date", "first_seen_at": "not-a-date"},
        })
        with TestClient(self.app) as client:
            response = client.get("/jobs", params={"sort": "newest"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()["jobs"]], [fallback_id, unknown_id])

    def test_newest_breaks_equal_effective_timestamp_ties_by_job_id(self):
        first_id, second_id = self.job_ids
        self._set_attributes({
            first_id: {"reported_posted_at": "2026-10-05T12:00:00Z"},
            second_id: {"reported_posted_at": "2026-10-05T07:00:00-05:00"},
        })
        with TestClient(self.app) as client:
            response = client.get("/jobs", params={"sort": "newest"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()["jobs"]], [first_id, second_id])

    def test_company_sort_is_case_insensitive_and_applies_limit_after_ordering(self):
        first_id, second_id = self.job_ids
        self._set_company_names({first_id: "Zulu Systems", second_id: "alpha Systems"})
        with TestClient(self.app) as client:
            response = client.get("/jobs", params={"sort": "company", "limit": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["id"] for row in response.json()["jobs"]], [second_id])

    def test_company_sort_places_blank_names_after_named_and_breaks_ties_by_job_id(self):
        first_id, second_id = self.job_ids
        self._set_company_names({first_id: "  ", second_id: "Acme Systems"})
        with TestClient(self.app) as client:
            named_before_blank = client.get("/jobs", params={"sort": "company"})
        self.assertEqual(named_before_blank.status_code, 200, named_before_blank.text)
        self.assertEqual([row["id"] for row in named_before_blank.json()["jobs"]], [second_id, first_id])

        self._set_company_names({first_id: "Acme Systems", second_id: "acme systems"})
        with TestClient(self.app) as client:
            tied = client.get("/jobs", params={"sort": "company"})
        self.assertEqual(tied.status_code, 200, tied.text)
        self.assertEqual([row["id"] for row in tied.json()["jobs"]], self.job_ids)


if __name__ == "__main__":
    unittest.main()
