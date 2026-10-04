"""Job lifecycle migration preserves legacy jobs and historical evidence."""
import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from packages.persistence import models as m


class JobLifecycleMigrationTests(unittest.TestCase):
    revision = "e9024a1b7c30"

    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def _legacy_rows(self, engine):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO companies (id,name,domain) VALUES "
                "('company','Employer','employer.example')"
            ))
            connection.execute(text(
                "INSERT INTO company_job_sources "
                "(id,company_id,provider,tenant,endpoint,discovered_automatically,last_success) "
                "VALUES ('source','company','rss','tenant','https://employer.example/feed',1,"
                "'2026-10-03T00:00:00Z')"
            ))
            connection.execute(text(
                "INSERT INTO canonical_jobs "
                "(id,company_id,requisition,title,description,skills,location,remote_type) "
                "VALUES ('job','company','req-1','Network Administrator','Python systems','[]','Remote','REMOTE')"
            ))
            connection.execute(text(
                "INSERT INTO job_postings "
                "(id,job_id,source,source_job_id,source_url,application_url,raw,fetched_at) "
                "VALUES ('posting','job','company-feed:source','req-1','https://employer.example/jobs',"
                "'https://employer.example/jobs/1','{}','2026-10-03T00:00:00Z')"
            ))
            connection.execute(text(
                "INSERT INTO posting_snapshots "
                "(id,posting_id,raw,sha256,retrieved_at) VALUES "
                "('snapshot','posting','{}','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
                "'2026-10-03T00:00:00Z')"
            ))
            connection.execute(text(
                "INSERT INTO source_quality_observations "
                "(id,company_source_id,observed_at,succeeded,available,enumerated,accepted,completeness_sum,salary_known) "
                "VALUES ('quality','source','2026-10-03T00:00:00Z',1,1,1,1,1.0,1)"
            ))
            connection.execute(text(
                "INSERT INTO posting_value_candidates "
                "(id,posting_id,posting_snapshot_id,company_source_id,quality_observation_id,observed_at,normalized,application_url) "
                "VALUES ('candidate','posting','snapshot','source','quality','2026-10-03T00:00:00Z',"
                "'{\"title\":\"Network Administrator\"}','https://employer.example/jobs/1')"
            ))

    def _migrate_legacy(self, directory):
        url = f"sqlite:///{(Path(directory) / 'legacy.db').as_posix()}"
        config = self._config(url)
        engine = create_engine(url)
        command.upgrade(config, "d8f0c1b2a3e4")
        self._legacy_rows(engine)
        command.upgrade(config, self.revision)
        return config, engine

    def test_upgrade_preserves_legacy_rows_and_matches_declared_model_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            config, engine = self._migrate_legacy(directory)
            try:
                database = inspect(engine)
                for table in (m.job_lifecycle_observations, m.job_description_revisions):
                    actual_columns = {
                        column["name"]: column for column in database.get_columns(table.name)
                    }
                    self.assertEqual(
                        [column.name for column in table.columns],
                        list(actual_columns),
                    )
                    for column in table.columns:
                        actual = actual_columns[column.name]
                        self.assertEqual(actual["nullable"], column.nullable, column.name)
                        self.assertEqual(str(actual["type"]).lower(), str(column.type).lower(), column.name)
                    self.assertEqual(
                        {foreign_key.target_fullname for column in table.columns for foreign_key in column.foreign_keys},
                        {foreign_key["referred_table"] + "." + foreign_key["referred_columns"][0]
                         for foreign_key in database.get_foreign_keys(table.name)},
                    )
                self.assertEqual(
                    {index.name for index in m.job_lifecycle_observations.indexes},
                    {index["name"] for index in database.get_indexes("job_lifecycle_observations")},
                )
                self.assertEqual(
                    {constraint.name for constraint in m.job_lifecycle_observations.constraints
                     if constraint.name},
                    {constraint["name"] for constraint in database.get_check_constraints("job_lifecycle_observations")
                     if constraint["name"]},
                )
                self.assertEqual(
                    {tuple(constraint.columns.keys()) for constraint in m.job_description_revisions.constraints
                     if constraint.__class__.__name__ == "UniqueConstraint"},
                    {tuple(constraint["column_names"]) for constraint in database.get_unique_constraints(
                        "job_description_revisions")},
                )
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.companies.c.id)).scalar_one(), "company")
                    self.assertEqual(connection.execute(select(m.jobs.c.id)).scalar_one(), "job")
                    self.assertEqual(connection.execute(select(m.job_lifecycle_observations)).all(), [])
                    self.assertEqual(connection.execute(select(m.job_description_revisions)).all(), [])
                command.downgrade(config, "d8f0c1b2a3e4")
            finally:
                engine.dispose()

    def test_empty_downgrade_drops_both_history_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            config, engine = self._migrate_legacy(directory)
            try:
                command.downgrade(config, "d8f0c1b2a3e4")
                self.assertNotIn("job_lifecycle_observations", inspect(engine).get_table_names())
                self.assertNotIn("job_description_revisions", inspect(engine).get_table_names())
            finally:
                engine.dispose()

    def test_populated_lifecycle_history_refuses_before_any_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            config, engine = self._migrate_legacy(directory)
            try:
                with engine.begin() as connection:
                    connection.execute(m.job_lifecycle_observations.insert().values(
                        id="lifecycle", job_id="job", posting_id="posting", candidate_id="candidate",
                        quality_observation_id="quality", observed_at="2026-10-03T00:00:00Z",
                        status="ACTIVE", authority="VERIFIED_EMPLOYER", evidence={"source": "feed"}))
                with self.assertRaisesRegex(RuntimeError, "Refusing to discard job lifecycle history"):
                    command.downgrade(config, "d8f0c1b2a3e4")
                self.assertIn("job_description_revisions", inspect(engine).get_table_names())
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.job_lifecycle_observations.c.id)).scalar_one(), "lifecycle")
            finally:
                engine.dispose()

    def test_populated_description_history_refuses_before_any_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            config, engine = self._migrate_legacy(directory)
            try:
                with engine.begin() as connection:
                    connection.execute(m.job_description_revisions.insert().values(
                        id="revision", job_id="job", sequence=1, candidate_id="candidate",
                        description_hash="a" * 64, material_hash="b" * 64,
                        description="Network Administrator", salary={}, remote_policy={},
                        recorded_at="2026-10-03T00:00:00Z"))
                with self.assertRaisesRegex(RuntimeError, "Refusing to discard job description history"):
                    command.downgrade(config, "d8f0c1b2a3e4")
                self.assertIn("job_lifecycle_observations", inspect(engine).get_table_names())
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.job_description_revisions.c.id)).scalar_one(), "revision")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
