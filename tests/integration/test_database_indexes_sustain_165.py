"""Behavioral database-index evidence for MJ-REQ-164 / issue #165.

The requirement lists candidate concepts, not mandatory physical column names.
These tests therefore bind index expectations to production query shapes and
verify plans on selective synthetic data.  They deliberately do not require
standalone indexes for JSON-backed filters or columns that are not predicates.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from packages.persistence import models as m
from packages.persistence.store import Store


ROW_COUNT = 2048
TARGET_COMPANY = "company-000"
TARGET_JOB = "job-0000"
TARGET_PROFILE = "profile-000"
TARGET_VERSION = "version-000"
PARENT_REVISION = "20261024dc01"
TARGET_REVISION = "20261025di01"


# These are the relational access paths exercised by production code.  Index
# names are intentionally absent: the contract is the usable column prefix.
REQUIRED_INDEX_PREFIXES = {
    "canonical_jobs": ("company_id", "id"),
    "job_matches": ("job_id", "profile_id", "profile_version"),
    "applications": ("profile_id", "created_at", "id"),
}


def _rows():
    companies = [{
        "id": f"company-{number:03d}",
        "name": f"Synthetic Company {number:03d}",
        "domain": f"company-{number:03d}.example",
    } for number in range(64)]
    profiles = [{
        "id": f"profile-{number:03d}",
        "name": f"Synthetic Profile {number:03d}",
        "email": f"profile-{number:03d}@example.invalid",
        "version": f"version-{number:03d}",
    } for number in range(64)]
    jobs, routes, matches, applications, sources, postings = [], [], [], [], [], []
    for number in range(ROW_COUNT):
        company_id = f"company-{number % 64:03d}"
        profile_id = f"profile-{number % 64:03d}"
        profile_version = f"version-{number % 64:03d}"
        job_id = f"job-{number:04d}"
        route_id = f"route-{number:04d}"
        timestamp = f"2026-10-{1 + number % 24:02d}T{number % 24:02d}:00:00+00:00"
        jobs.append({
            "id": job_id,
            "company_id": company_id,
            "requisition": f"req-{number:04d}",
            "title": f"Synthetic Engineer {number:04d}",
            "description": "Value-free fixture role",
            "skills": [],
            "location": "Fixture",
            "remote_type": "REMOTE" if number % 2 == 0 else "ONSITE",
            # These requirement concepts are queried through JSON today.  They
            # are present so the plan evidence reflects the real representation.
            "attributes": {
                "normalized_title": "synthetic engineer",
                "reported_posted_at": timestamp,
                "first_seen_at": timestamp,
                "salary_min": 50_000 + number,
                "salary_max": 70_000 + number,
                "salary_currency": "USD",
                "salary_period": "YEAR",
                "source_id": f"source-{number % 8}",
            },
        })
        routes.append({
            "id": route_id,
            "job_id": job_id,
            "company_id": company_id,
            "provider": "mock-ats",
            "tenant": company_id,
            "requisition": f"req-{number:04d}",
            "url": f"https://fixture.invalid/jobs/{number}",
        })
        matches.append({
            "id": f"match-{number:04d}",
            "job_id": job_id,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "score": float(number % 101),
            "missing": [],
            "components": {},
        })
        applications.append({
            "id": f"application-{number:04d}",
            "job_id": job_id,
            "route_id": route_id,
            "profile_id": profile_id,
            "profile_version": profile_version,
            "state": "PREPARED",
            "target": "MOCK",
            "fact_ids": [],
            "trace": [],
            "document_version": 1,
            "created_at": timestamp,
        })
        sources.append({
            "id": f"company-source-{number:04d}",
            "company_id": company_id,
            "provider": "mock-ats",
            "tenant": f"tenant-{number:04d}",
            "endpoint": f"https://fixture.invalid/api/{number}",
            "discovered_automatically": True,
            "origin": "AUTO_DISCOVERED",
            "career_url": f"https://fixture.invalid/careers/{number}",
            "collection_strategy": "FIXTURE",
            "confidence": 1.0,
            # Sparse selection makes a partial enabled-source index falsifiable.
            "enabled": number % 128 == 0,
            "priority": "USER_PINNED" if number % 257 == 0 else "NORMAL",
        })
        postings.append({
            "id": f"posting-{number:04d}",
            "job_id": job_id,
            "source": f"source-{number % 8}",
            "source_job_id": f"source-job-{number:04d}",
            "source_url": f"https://fixture.invalid/source/{number}",
            "application_url": f"https://fixture.invalid/apply/{number}",
            "raw": {},
            "fetched_at": timestamp,
        })
    tenants = [{
        "id": f"ats-{number:04d}",
        "company_id": f"company-{number % 64:03d}",
        "provider": "mock-ats",
        "tenant_identifier": f"tenant-{number:04d}",
        "organization_identifier": "fixture",
        "board_identifier": "jobs",
        "confidence": 1.0,
        "evidence": {},
        "first_seen_at": "2026-10-01T00:00:00+00:00",
        "last_seen_at": "2026-10-01T00:00:00+00:00",
    } for number in range(ROW_COUNT)]
    return companies, profiles, jobs, routes, matches, applications, tenants, sources, postings


def _seed(engine):
    companies, profiles, jobs, routes, matches, applications, tenants, sources, postings = _rows()
    with engine.begin() as connection:
        connection.execute(m.companies.insert(), companies)
        connection.execute(m.profiles.insert(), profiles)
        connection.execute(m.jobs.insert(), jobs)
        connection.execute(m.routes.insert(), routes)
        connection.execute(m.matches.insert(), matches)
        connection.execute(m.applications.insert(), applications)
        connection.execute(m.company_ats_tenants.insert(), tenants)
        connection.execute(m.company_sources.insert(), sources)
        connection.execute(m.postings.insert(), postings)
    with engine.begin() as connection:
        connection.exec_driver_sql("ANALYZE")


def _index_records(engine, table_name):
    return inspect(engine).get_indexes(table_name)


def _index_columns(record):
    return tuple(column for column in record.get("column_names", ()) if column is not None)


def _has_prefix(engine, table_name, prefix):
    return any(_index_columns(index)[:len(prefix)] == tuple(prefix)
               for index in _index_records(engine, table_name))


def _index_definition(engine, table_name, prefix):
    record = next(
        index for index in _index_records(engine, table_name)
        if _index_columns(index)[:len(prefix)] == tuple(prefix)
    )
    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            definition = connection.execute(text(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"
            ), {"name": record["name"]}).scalar_one()
        else:
            definition = connection.execute(text(
                "SELECT pg_get_indexdef(to_regclass(:name))"
            ), {"name": record["name"]}).scalar_one()
    return " ".join(definition.lower().split())


def _partial_enabled_index(engine):
    for index in _index_records(engine, "company_job_sources"):
        if _index_columns(index)[:1] != ("id",):
            continue
        options = index.get("dialect_options") or {}
        clause = options.get("sqlite_where")
        if clause is None:
            clause = options.get("postgresql_where")
        predicate = str(clause or "").lower() if clause is None else str(clause).lower()
        if "enabled" in predicate:
            return True
    return False


def _sqlite_explain(engine, sql, params):
    # This is how Column.is_(True) compiles for SQLite; partial-index
    # predicates must be syntactically equivalent for SQLite to select them.
    sql = sql.replace("enabled IS TRUE", "enabled IS 1")
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params).all()
    return "\n".join(str(row[3]) for row in rows)


def _assert_sqlite_plan(testcase, label, relation, plan, ordered):
    normalized = plan.upper()
    if label == "enabled source scheduling":
        testcase.assertIn("USING", normalized,
                          f"{label} did not use the selective partial index:\n{plan}")
        testcase.assertNotIn("SQLITE_AUTOINDEX", normalized,
                             f"{label} scanned an unrelated full uniqueness index:\n{plan}")
    else:
        testcase.assertIn(f"SEARCH {relation.upper()} USING", normalized,
                          f"{label} did not perform a selective index search:\n{plan}")
    if ordered:
        testcase.assertNotIn("USE TEMP B-TREE FOR ORDER BY", normalized,
                             f"{label} required an avoidable sort:\n{plan}")


QUERY_CASES = {
    "canonical company listing": (
        "canonical_jobs",
        "SELECT id FROM canonical_jobs WHERE company_id = :company_id ORDER BY id",
        {"company_id": TARGET_COMPANY},
        True,
    ),
    "active profile match": (
        "job_matches",
        "SELECT score FROM job_matches WHERE job_id = :job_id "
        "AND profile_id = :profile_id AND profile_version = :profile_version",
        {"job_id": TARGET_JOB, "profile_id": TARGET_PROFILE,
         "profile_version": TARGET_VERSION},
        False,
    ),
    "recent profile applications": (
        "applications",
        "SELECT id FROM applications WHERE profile_id = :profile_id "
        "ORDER BY created_at DESC, id DESC LIMIT 200",
        {"profile_id": TARGET_PROFILE},
        True,
    ),
    "company source listing": (
        "company_job_sources",
        "SELECT id FROM company_job_sources WHERE company_id = :company_id "
        "ORDER BY provider, tenant, id",
        {"company_id": TARGET_COMPANY},
        # The existing uniqueness index supplies the company/provider/tenant
        # search prefix. Eliminating this presentation sort is not one of the
        # evidence-backed indexes authorized by the production query audit.
        False,
    ),
    "enabled source scheduling": (
        "company_job_sources",
        "SELECT id FROM company_job_sources WHERE enabled IS TRUE ORDER BY id",
        {},
        True,
    ),
    "ATS ownership lookup": (
        "company_ats_tenants",
        "SELECT id FROM company_ats_tenants WHERE company_id = :company_id "
        "AND provider = 'mock-ats' AND tenant_identifier = 'tenant-0000'",
        {"company_id": TARGET_COMPANY},
        False,
    ),
    "source posting lookup": (
        "job_postings",
        "SELECT id FROM job_postings WHERE source = 'source-0' "
        "AND source_job_id = 'source-job-0000'",
        {},
        False,
    ),
}


class DatabaseIndexContractMixin:
    engine = None

    def test_requirement_concepts_are_bound_to_real_storage(self):
        self.assertFalse({"status", "normalized_title", "posted_at", "first_seen_at", "salary"}
                         & set(m.jobs.c.keys()))
        self.assertIn("attributes", m.jobs.c)
        self.assertIn("remote_type", m.jobs.c)
        self.assertIn("score", m.matches.c)
        self.assertIn("state", m.applications.c)
        self.assertIn("source", m.postings.c)
        self.assertIn("provider", m.company_ats_tenants.c)
        self.assertIn("priority", m.company_sources.c)
        # Status history is retrieved per job/time, for which an existing index
        # already supplies the actual relational access path.
        self.assertTrue(_has_prefix(self.engine, "job_lifecycle_observations",
                                    ("job_id", "observed_at")))

    def test_evidence_backed_index_shapes_exist(self):
        missing = {
            table: prefix for table, prefix in REQUIRED_INDEX_PREFIXES.items()
            if not _has_prefix(self.engine, table, prefix)
        }
        self.assertEqual(missing, {}, f"Missing production-query index prefixes: {missing}")
        application_definition = _index_definition(
            self.engine, "applications", REQUIRED_INDEX_PREFIXES["applications"],
        )
        self.assertIn("(profile_id, created_at desc, id desc)", application_definition)
        self.assertTrue(
            _partial_enabled_index(self.engine),
            "Enabled-source scheduling needs a selective partial index on id",
        )

    def explain(self, sql, params):
        raise NotImplementedError

    def assert_uses_index(self, label, relation, plan, ordered):
        raise NotImplementedError

    def test_selective_production_queries_use_indexes(self):
        for label, (relation, sql, params, ordered) in QUERY_CASES.items():
            with self.subTest(query=label):
                plan = self.explain(sql, params)
                self.assert_uses_index(label, relation, plan, ordered)


class DatabaseIndexSQLiteTests(DatabaseIndexContractMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory()
        cls.store = Store("sqlite:///" + str(Path(cls._temp.name) / "indexes.db"))
        cls.store.migrate()
        cls.engine = cls.store.engine
        _seed(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        cls._temp.cleanup()

    def explain(self, sql, params):
        return _sqlite_explain(self.engine, sql, params)

    def assert_uses_index(self, label, relation, plan, ordered):
        _assert_sqlite_plan(self, label, relation, plan, ordered)


class DatabaseIndexSQLiteMigrationTests(unittest.TestCase):
    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _migrate(self, config, url, revision, *, downgrade=False):
        with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
            if downgrade:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)

    def _snapshot(self, engine):
        with engine.connect() as connection:
            return {
                table: connection.execute(text(
                    f'SELECT COUNT(*), MIN(id), MAX(id) FROM "{table}"'
                )).one()
                for table in (
                    "companies", "career_profiles", "canonical_jobs", "application_routes",
                    "job_matches", "applications", "company_ats_tenants",
                    "company_job_sources", "job_postings",
                )
            }

    def _assert_target_schema_and_plans(self, engine):
        for table, prefix in REQUIRED_INDEX_PREFIXES.items():
            self.assertTrue(_has_prefix(engine, table, prefix), (table, prefix))
        self.assertTrue(_partial_enabled_index(engine))
        for label, (relation, sql, params, ordered) in QUERY_CASES.items():
            with self.subTest(query=label):
                _assert_sqlite_plan(
                    self, label, relation, _sqlite_explain(engine, sql, params), ordered,
                )

    def test_upgrade_downgrade_reupgrade_preserves_rows_schema_and_plans(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "migration.db"
            url = "sqlite:///" + str(path)
            config = self._config(url)
            self._migrate(config, url, PARENT_REVISION)
            engine = create_engine(url)
            try:
                _seed(engine)
                original = self._snapshot(engine)
            finally:
                engine.dispose()

            self._migrate(config, url, TARGET_REVISION)
            upgraded = create_engine(url)
            try:
                self.assertEqual(self._snapshot(upgraded), original)
                self._assert_target_schema_and_plans(upgraded)
                with upgraded.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), TARGET_REVISION)
            finally:
                upgraded.dispose()

            self._migrate(config, url, PARENT_REVISION, downgrade=True)
            downgraded = create_engine(url)
            try:
                self.assertEqual(self._snapshot(downgraded), original)
                for table, prefix in REQUIRED_INDEX_PREFIXES.items():
                    self.assertFalse(_has_prefix(downgraded, table, prefix), (table, prefix))
                self.assertFalse(_partial_enabled_index(downgraded))
                with downgraded.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), PARENT_REVISION)
            finally:
                downgraded.dispose()

            self._migrate(config, url, TARGET_REVISION)
            reopened = create_engine(url)
            try:
                self.assertEqual(self._snapshot(reopened), original)
                self._assert_target_schema_and_plans(reopened)
                with reopened.connect() as connection:
                    self.assertEqual(connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one(), TARGET_REVISION)
            finally:
                reopened.dispose()


POSTGRES_URL = os.environ.get("MJOBS_TEST_POSTGRES_URL")


def _disposable_postgres_url(value):
    url = make_url(value)
    if (not url.drivername.startswith("postgresql")
            or url.host not in ("localhost", "127.0.0.1", "::1")
            or not url.database
            or not (url.database == "mjobs_test" or url.database.startswith("mjobs_test_"))):
        raise RuntimeError("PostgreSQL index evidence requires loopback mjobs_test[_*] database")
    return url


class DatabaseIndexPostgreSQLGuardTests(unittest.TestCase):
    def test_standard_ci_fixture_name_is_admitted_without_weakening_boundary(self):
        self.assertEqual(
            _disposable_postgres_url(
                "postgresql://fixture:fixture@127.0.0.1:5432/mjobs_test"
            ).database,
            "mjobs_test",
        )
        self.assertEqual(
            _disposable_postgres_url(
                "postgresql://fixture:fixture@localhost:5432/mjobs_test_indexes"
            ).database,
            "mjobs_test_indexes",
        )
        for unsafe in (
            "postgresql://fixture:fixture@database.example/mjobs_test",
            "postgresql://fixture:fixture@127.0.0.1/production",
            "sqlite:///mjobs_test",
        ):
            with self.subTest(url=unsafe), self.assertRaisesRegex(RuntimeError, "loopback"):
                _disposable_postgres_url(unsafe)


@unittest.skipUnless(POSTGRES_URL, "Requires isolated disposable PostgreSQL")
class DatabaseIndexPostgreSQLTests(DatabaseIndexContractMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = _disposable_postgres_url(POSTGRES_URL)
        cls.schema = "database_indexes_" + uuid4().hex
        cls.admin = create_engine(url)
        with cls.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{cls.schema}"')
        scoped = url.update_query_dict({
            "options": "-csearch_path=" + cls.schema + " -clock_timeout=5000 -cstatement_timeout=30000",
        })
        cls.engine = create_engine(scoped)
        from packages.persistence import resume_workspace  # noqa: F401
        m.metadata.create_all(cls.engine)
        _seed(cls.engine)

    @classmethod
    def tearDownClass(cls):
        try:
            if (not cls.schema.startswith("database_indexes_") or len(cls.schema) != 49
                    or any(character not in "0123456789abcdef" for character in cls.schema[17:])):
                raise ValueError("Unexpected disposable schema identifier")
            with cls.admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{cls.schema}" CASCADE')
        finally:
            cls.engine.dispose()
            cls.admin.dispose()

    def explain(self, sql, params):
        with self.engine.connect() as connection:
            # text() translates SQLAlchemy's named binds to psycopg's native
            # parameter style; exec_driver_sql() intentionally does not.
            rows = connection.execute(text("EXPLAIN (COSTS OFF) " + sql), params).all()
        return "\n".join(str(row[0]) for row in rows)

    def assert_uses_index(self, label, relation, plan, _ordered):
        normalized = plan.lower()
        self.assertTrue(
            any(kind in normalized for kind in ("index scan", "index only scan", "bitmap index scan")),
            f"{label} did not use an index:\n{plan}",
        )
        self.assertNotIn(f"seq scan on {relation}", normalized,
                         f"{label} performed a sequential scan:\n{plan}")


if __name__ == "__main__":
    unittest.main()
