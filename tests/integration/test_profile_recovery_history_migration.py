"""Additive inert-history table migration preserves history on downgrade."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from packages.persistence import models as m


class ProfileRecoveryHistoryMigrationTests(unittest.TestCase):
    expected_dialect = "sqlite"

    def database_url(self, directory, filename):
        return f"sqlite:///{(Path(directory) / filename).as_posix()}"

    def _config(self, url):
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config

    def test_upgrade_is_additive_and_empty_downgrade_removes_only_snapshot_table(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-recovery-migration-") as directory:
            url = self.database_url(directory, "migration.db")
            config = self._config(url)
            engine = create_engine(url, poolclass=NullPool)
            try:
                self.assertEqual(engine.dialect.name, self.expected_dialect)
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "20261013ai01")
                    with engine.begin() as connection:
                        connection.execute(m.profiles.insert().values(
                            id="migration-profile", name="Synthetic", email="synthetic@example.invalid",
                            version="v1"))
                    command.upgrade(config, "head")
                    with engine.connect() as connection:
                        self.assertEqual(connection.execute(select(m.profile_recovery_history_snapshots)).all(), [])
                        self.assertEqual(connection.execute(select(m.company_discovery_requests)).all(), [])
                        foreign_keys = inspect(connection).get_foreign_keys(
                            "profile_recovery_history_snapshots")
                        self.assertEqual([item["referred_table"] for item in foreign_keys],
                                         ["career_profiles"])
                    command.downgrade(config, "20261013ai01")
                    with engine.connect() as connection:
                        tables = inspect(connection).get_table_names()
                        self.assertNotIn("profile_recovery_history_snapshots", tables)
                        self.assertIn("company_discovery_requests", tables)
            finally:
                engine.dispose()

    def test_populated_archive_refuses_downgrade_without_losing_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-recovery-migration-") as directory:
            url = self.database_url(directory, "preserve.db")
            config = self._config(url)
            engine = create_engine(url, poolclass=NullPool)
            try:
                self.assertEqual(engine.dialect.name, self.expected_dialect)
                with patch.dict(os.environ, {"MJOBS_DATABASE_URL": url}):
                    command.upgrade(config, "20261013ai01")
                    with engine.begin() as connection:
                        connection.execute(m.profiles.insert().values(
                            id="migration-profile", name="Synthetic", email="synthetic@example.invalid",
                            version="v1"))
                    command.upgrade(config, "head")
                    with engine.begin() as connection:
                        connection.execute(m.profile_recovery_history_snapshots.insert().values(
                            id="archive-1", profile_id="migration-profile",
                            kind="company_discovery_request", source_identity="source-1",
                            schema_version=1, payload={"state": "UNKNOWN"}, created_at=""))
                    with self.assertRaisesRegex(RuntimeError, "Refusing to discard recovered profile history"):
                        command.downgrade(config, "20261013ai01")
                    with engine.connect() as connection:
                        row = connection.execute(select(m.profile_recovery_history_snapshots)).mappings().one()
                        self.assertEqual(row["source_identity"], "source-1")
                        self.assertEqual(connection.execute(select(m.company_discovery_requests)).all(), [])
            finally:
                engine.dispose()


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Requires the disposable MJOBS_TEST_POSTGRES_URL PostgreSQL fixture")
class PostgreSQLProfileRecoveryHistoryMigrationTests(ProfileRecoveryHistoryMigrationTests):
    expected_dialect = "postgresql"

    def setUp(self):
        url = _disposable_postgres_url(os.environ["MJOBS_TEST_POSTGRES_URL"])
        self.admin = create_engine(url, poolclass=NullPool)
        self.schema = "profile_recovery_history_" + uuid4().hex
        self.addCleanup(self.cleanup_schema)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.postgres_url = url.update_query_dict({
            "options": "-csearch_path=" + self.schema,
        }).render_as_string(hide_password=False)

    def cleanup_schema(self):
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def database_url(self, directory, filename):
        return self.postgres_url

    def _config(self, url):
        config = super()._config(url)
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        return config


def _disposable_postgres_url(value):
    url = make_url(value)
    if (not url.drivername.startswith("postgresql")
            or url.host not in ("localhost", "127.0.0.1", "::1")
            or url.query
            or not url.database
            or not url.database.startswith("mjobs_test")):
        raise RuntimeError("Requires a disposable loopback mjobs_test PostgreSQL URL without query overrides")
    return url


class PostgreSQLMigrationAdmissionTests(unittest.TestCase):
    def test_disposable_url_requires_loopback_database_without_query_overrides(self):
        accepted = _disposable_postgres_url(
            "postgresql+psycopg://synthetic:synthetic@127.0.0.1:5432/mjobs_test",
        )
        self.assertEqual(accepted.database, "mjobs_test")
        for value in (
                "postgresql+psycopg://synthetic:synthetic@example.test/mjobs_test",
                "postgresql+psycopg://synthetic:synthetic@127.0.0.1/production",
                "postgresql+psycopg://synthetic:synthetic@127.0.0.1/mjobs_test?options=-csearch_path=private",
        ):
            with self.subTest(url_class="external_or_overridden"), self.assertRaisesRegex(
                    RuntimeError, "disposable loopback"):
                _disposable_postgres_url(value)


if __name__ == "__main__":
    unittest.main()
