"""Company-source monitoring migration preserves history and refuses evidence loss."""
import tempfile
import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text

from packages.persistence import models as m


class CompanySourceMonitoringMigrationTests(unittest.TestCase):
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

    def test_upgrade_preserves_sources_without_fabricating_monitoring_and_empty_downgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'legacy.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                command.upgrade(config, "c3e7a1b50d62")
                self._legacy_rows(engine)
                command.upgrade(config, "head")
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(select(m.company_sources.c.id)).scalar_one(), "source")
                    self.assertEqual(connection.execute(select(m.company_source_monitoring)).all(), [])
                    row = connection.execute(
                        text("SELECT revision, enabled, poll_interval, status FROM company_source_monitoring")
                    ).first()
                    self.assertIsNone(row)
                command.downgrade(config, "c3e7a1b50d62")
            finally:
                engine.dispose()

    def test_populated_monitoring_refuses_downgrade_and_preserves_row(self):
        with tempfile.TemporaryDirectory() as directory:
            url = f"sqlite:///{(Path(directory) / 'monitoring.db').as_posix()}"
            config = self._config(url)
            engine = create_engine(url)
            try:
                command.upgrade(config, "c3e7a1b50d62")
                self._legacy_rows(engine)
                command.upgrade(config, "head")
                with engine.begin() as connection:
                    connection.execute(m.company_source_monitoring.insert().values(
                        id="monitoring", source_id="source",
                        source_snapshot={"endpoint": "https://employer.example/feed"},
                        authorization="VERIFIED_DOMAIN",
                        domain_evidence={"method": "existing_verified_domain"},
                        created_at="2026-10-03T00:00:00Z",
                        updated_at="2026-10-03T00:00:00Z",
                    ))
                with self.assertRaisesRegex(RuntimeError, "Refusing to discard company source monitoring state"):
                    command.downgrade(config, "c3e7a1b50d62")
                with engine.connect() as connection:
                    row = connection.execute(select(m.company_source_monitoring)).mappings().one()
                    self.assertEqual(row["id"], "monitoring")
                    self.assertEqual(row["source_id"], "source")
                    self.assertEqual(row["revision"], 1)
                    self.assertTrue(row["enabled"])
                    self.assertEqual(row["poll_interval"], 3600)
                    self.assertEqual(row["status"], "IDLE")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
