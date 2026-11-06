"""Salary queries reject malformed JSON on SQLite and disposable PostgreSQL."""
import os
import sys
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, text

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence import resume_workspace  # noqa: F401
from packages.persistence.store import Store
from tests.integration.test_lever_form_review_flow import _disposable_postgres_url
from tests.integration.test_spine import PROFILE


POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")


class _SalaryQueryContract:
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mjobs-salary-query-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = self.new_store()
        self.addCleanup(self.store.engine.dispose)
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.app = create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        )
        self.expected = {}
        for period, low, high in (("YEAR", 80000, 120000), ("HOUR", 25, 45)):
            base = {"salary_period": period, "salary_currency": "USD"}
            self.add_job(period + "-range", {**base, "salary_min": low, "salary_max": high})
            self.add_job(period + "-low", {**base, "salary_min": low, "salary_max": None})
            self.add_job(period + "-high", {**base, "salary_min": None, "salary_max": high})
            self.add_job(period + "-negative", {**base, "salary_min": -1, "salary_max": high})
            self.add_job(period + "-inverted", {**base, "salary_min": high, "salary_max": low})
            self.add_job(period + "-estimated", {
                **base, "salary_min": low, "salary_max": high, "salary_source": "ESTIMATED",
            })
            self.add_job(period + "-foreign", {
                **base, "salary_min": low, "salary_max": high, "salary_currency": "CAD",
            })
            malformed_values = (
                "not-a-number", "90000", True, [90000], {"amount": 90000},
                10 ** 400, -(10 ** 400),
            )
            for index, malformed in enumerate(malformed_values):
                for endpoint in ("salary_min", "salary_max"):
                    self.add_job(f"{period}-malformed-{endpoint}-{index}", {
                        **base, "salary_min": low, "salary_max": high, endpoint: malformed,
                    })
        self.add_job("missing", {})
        self.add_job("null", {"salary_min": None, "salary_max": None})

    def add_job(self, name, attributes):
        job_id = "salary-safety-" + name
        self.expected[job_id] = attributes
        with self.store.engine.begin() as connection:
            connection.execute(insert(m.companies).values(
                id=job_id, name="Synthetic Salary Employer", domain="",
                parent_company_id=None, relationship_type=None,
            ))
            connection.execute(insert(m.jobs).values(
                id=job_id, company_id=job_id, requisition=job_id, title="Salary " + name,
                description="Synthetic query-safety fixture", skills=[], location="Remote",
                remote_type="REMOTE", attributes=attributes,
            ))
            connection.execute(insert(m.routes).values(
                id=job_id, job_id=job_id, company_id=job_id, provider="mock-ats",
                tenant="salary-query-fixture", requisition=job_id,
                url="https://example.invalid/apply",
            ))

    def ids_for(self, **params):
        with TestClient(self.app, raise_server_exceptions=False) as client:
            response = client.get("/jobs", params={"sort": "title", **params})
        self.assertEqual(response.status_code, 200, response.text)
        return [row["id"] for row in response.json()["jobs"]]

    def assert_source_unchanged(self):
        with self.store.engine.connect() as connection:
            current = dict(connection.execute(select(m.jobs.c.id, m.jobs.c.attributes)).all())
        self.assertEqual(current, self.expected)

    def test_malformed_endpoints_never_break_hourly_or_annual_bounds(self):
        for period, minimum, maximum in (("YEAR", 90000, 110000), ("HOUR", 30, 40)):
            with self.subTest(period=period):
                self.assertEqual(self.ids_for(
                    salary_period=period, salary_min=minimum, salary_max=maximum, limit=1,
                ), ["salary-safety-" + period + "-range"])
        self.assert_source_unchanged()

    def test_unknown_opt_in_never_admits_malformed_or_incompatible_salary(self):
        for period, minimum, maximum in (("YEAR", 90000, 110000), ("HOUR", 30, 40)):
            with self.subTest(period=period):
                self.assertEqual(set(self.ids_for(
                    salary_period=period, salary_min=minimum, salary_max=maximum,
                    salary_unknown_allowed="true",
                )), {"salary-safety-" + period + "-range",
                     "salary-safety-missing", "salary-safety-null"})
        self.assert_source_unchanged()

    def test_known_only_preserves_valid_one_sided_bands(self):
        for period in ("YEAR", "HOUR"):
            with self.subTest(period=period):
                self.assertEqual(set(self.ids_for(
                    salary_period=period, salary_known_only="true",
                )), {"salary-safety-" + period + suffix for suffix in ("-range", "-low", "-high")})
        self.assert_source_unchanged()

    def test_salary_sort_keeps_malformed_values_after_comparable_annual_values(self):
        comparable = {"salary-safety-YEAR-" + name for name in ("range", "low", "high", "estimated")}
        for sort in ("salary_high", "salary_low"):
            with self.subTest(sort=sort):
                ordered = self.ids_for(sort=sort)
                self.assertEqual(set(ordered[:4]), comparable)
                self.assertEqual(set(ordered), set(self.expected))
                self.assertEqual(ordered[4:], sorted(set(self.expected) - comparable))
        self.assert_source_unchanged()

    def test_finite_float_boundary_remains_known_and_sortable(self):
        for period in ("YEAR", "HOUR"):
            with self.subTest(period=period):
                name = period + "-finite-boundary"
                self.add_job(name, {
                    "salary_currency": "USD", "salary_period": period,
                    "salary_min": 0, "salary_max": sys.float_info.max,
                })
                self.assertIn("salary-safety-" + name, self.ids_for(
                    salary_period=period, salary_known_only="true",
                ))
        self.assertEqual(self.ids_for(sort="salary_high")[0],
                         "salary-safety-YEAR-finite-boundary")
        self.assert_source_unchanged()

    def test_unfiltered_read_preserves_all_source_metadata(self):
        self.assertEqual(set(self.ids_for()), set(self.expected))
        self.assert_source_unchanged()


class SalaryQuerySQLiteTests(_SalaryQueryContract, unittest.TestCase):
    def new_store(self):
        store = Store(f"sqlite:///{(self.root / 'jobs.db').as_posix()}")
        store.migrate()
        return store


@unittest.skipUnless(POSTGRES_URL, "NOT_RUN: disposable loopback MJOBS_TEST_POSTGRES_URL absent")
class SalaryQueryPostgreSQLTests(_SalaryQueryContract, unittest.TestCase):
    def new_store(self):
        url = _disposable_postgres_url(POSTGRES_URL)
        self.schema = "mjobs_salary_" + uuid4().hex
        self.admin = create_engine(url, connect_args={"connect_timeout": 5})
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + self.schema + '"'))
        self.addCleanup(self.drop_schema)
        scoped = url.update_query_dict({
            "options": "-csearch_path=" + self.schema + " -clock_timeout=5000 -cstatement_timeout=30000",
        })
        store = Store(scoped.render_as_string(hide_password=False))
        self.addCleanup(store.engine.dispose)
        # The fixture exercises the current ORM/query contract. It does not
        # claim migration or production-schema acceptance.
        m.metadata.create_all(store.engine)
        return store

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + self.schema + '" CASCADE'))
